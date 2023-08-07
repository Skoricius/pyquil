##############################################################################
# Copyright 2016-2018 Rigetti Computing
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
##############################################################################
from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Dict, Optional, Union

import numpy as np
from numpy.typing import NDArray
from rpcq.messages import ParameterSpec

from pyquil.api import QuantumExecutable, EncryptedProgram

from pyquil.api._qam import MemoryMap, QAM, QAMExecutionResult
from pyquil.quilatom import (
    MemoryReference,
)
from qcs_sdk import QCSClient
from qcs_sdk.qpu.api import (
    submit,
    retrieve_results,
    ConnectionStrategy,
    ExecutionResult,
    ExecutionOptions,
    ExecutionOptionsBuilder,
)
from qcs_sdk.qpu.rewrite_arithmetic import build_patch_values


def decode_buffer(buffer: ExecutionResult) -> Union[NDArray[np.complex64], NDArray[np.int32]]:
    """
    Translate a DataBuffer into a numpy array.

    :param buffer: Dictionary with 'data' byte array, 'dtype', and 'shape' fields
    :return: NumPy array of decoded data
    """
    if buffer.dtype == "complex":
        return np.array(buffer.data.to_complex32(), dtype=np.complex64)
    elif buffer.dtype == "integer":
        return np.array(buffer.data.to_i32(), dtype=np.int32)
    return np.array([], np.int32)


def _extract_memory_regions(
    memory_descriptors: Dict[str, ParameterSpec],
    ro_sources: Dict[MemoryReference, str],
    buffers: Dict[str, np.ndarray],
) -> Dict[str, np.ndarray]:
    def alloc(spec: ParameterSpec, num_shots: int) -> np.ndarray:
        dtype = {
            "BIT": np.int64,
            "INTEGER": np.int64,
            "REAL": np.float64,
            "FLOAT": np.float64,
        }
        try:
            return np.ndarray((num_shots, spec.length), dtype=dtype[spec.type])
        except KeyError as e:
            raise ValueError(f"Unexpected memory type {spec.type}.") from e

    regions: Dict[str, np.ndarray] = {}

    # filter out the ro_sources to only get the ones that are used and group
    # mrefs by their name
    sources = {mem_desc: defaultdict(list) for mem_desc in memory_descriptors}
    for mref, key in ro_sources.items():
        # Translation sometimes introduces ro_sources that the user didn't
        # ask for. That's fine, we just ignore them.
        if mref.name not in sources:
            continue
        sources[mref.name][key].append(mref)

    # hack to extract the number of shots
    # the number of shots is the length of the buffer
    # divided by how many sources are in that buffer
    # it should be the same for all buffers
    mref_name = next(iter(sources))
    buf_key = next(iter(sources[mref_name]))
    num_shots = buffers[buf_key].shape[0] // len(sources[mref_name][buf_key])

    for mref_name, mref_dict in sources.items():
        regions[mref_name] = alloc(memory_descriptors[mref_name], num_shots)
        region_width = memory_descriptors[mref_name].length
        for key, mref_list in mref_dict.items():
            num_lanes = len(mref_list)
            # sort the items in the mref_list by their offset
            sorted_mref = [mref for _, mref in sorted(zip((m.offset for m in mref_list), mref_list))]
            buf = buffers[key]
            if buf.ndim == 1:
                buf = buf[:, None]
            if np.iscomplexobj(buf):
                buf = np.column_stack((buf.real, buf.imag))
            _, width = buf.shape
            for i, mref in enumerate(sorted_mref):
                end = mref.offset + width
                if end > region_width:
                    raise ValueError(
                        f"Attempted to fill {mref_name}[{mref.offset}, {end})"
                        f"but the declared region has width {region_width}."
                    )
                regions[mref_name][:, mref.offset : end] = buf[i::num_lanes, :]

    return regions


@dataclass
class QPUExecuteResponse:
    job_id: str
    _executable: EncryptedProgram
    execution_options: Optional[ExecutionOptions]


class QPU(QAM[QPUExecuteResponse]):
    def __init__(
        self,
        *,
        quantum_processor_id: str,
        priority: int = 1,
        timeout: Optional[float] = 30.0,
        client_configuration: Optional[QCSClient] = None,
        endpoint_id: Optional[str] = None,
        execution_options: Optional[ExecutionOptions] = None,
    ) -> None:
        """
        A connection to the QPU.

        :param quantum_processor_id: Processor to run against.
        :param priority: The priority with which to insert jobs into the QPU queue. Lower integers
            correspond to higher priority.
        :param timeout: Time limit for requests, in seconds.
        :param client_configuration: Optional client configuration. If none is provided, a default one will be loaded.
        :param endpoint_id: Optional endpoint ID to be used for execution.
        :param execution_options: The ``ExecutionOptions`` to use when executing a program. If provided, the options
            take precedence over the `timeout` and `endpoint_id` parameters.
        """
        super().__init__()

        self.priority = priority

        self._client_configuration = client_configuration or QCSClient.load()
        self._last_results: Dict[str, np.ndarray] = {}
        self._memory_results: Dict[str, Optional[np.ndarray]] = defaultdict(lambda: None)
        self._quantum_processor_id = quantum_processor_id
        if execution_options is None:
            execution_options_builder = ExecutionOptionsBuilder.default()
            execution_options_builder.timeout_seconds = timeout
            execution_options_builder.connection_strategy = ConnectionStrategy.default()
            if endpoint_id is not None:
                execution_options_builder.connection_strategy = ConnectionStrategy.endpoint_id(endpoint_id)
            execution_options = execution_options_builder.build()
        self.execution_options = execution_options

    @property
    def quantum_processor_id(self) -> str:
        """ID of quantum processor targeted."""
        return self._quantum_processor_id

    def execute(
        self,
        executable: QuantumExecutable,
        memory_map: Optional[MemoryMap] = None,
        execution_options: Optional[ExecutionOptions] = None,
        **__: Any,
    ) -> QPUExecuteResponse:
        """
        Enqueue a job for execution on the QPU. Returns a ``QPUExecuteResponse``, a
        job descriptor which should be passed directly to ``QPU.get_result`` to retrieve
        results.

        :param:
            execution_options: An optional `ExecutionOptions` enum that can be used
              to configure how the job is submitted and retrieved from the QPU. If unset,
              an appropriate default will be used.
        """
        executable = executable.copy()

        assert isinstance(
            executable, EncryptedProgram
        ), "QPU#execute requires an rpcq.EncryptedProgram. Create one with QuantumComputer#compile"

        assert (
            executable.ro_sources is not None
        ), "To run on a QPU, a program must include ``MEASURE``, ``CAPTURE``, and/or ``RAW-CAPTURE`` instructions"

        patch_values = build_patch_values(executable.recalculation_table, memory_map or {})

        job_id = submit(
            program=executable.program,
            patch_values=patch_values,
            quantum_processor_id=self.quantum_processor_id,
            client=self._client_configuration,
            execution_options=execution_options or self.execution_options,
        )

        return QPUExecuteResponse(_executable=executable, job_id=job_id, execution_options=execution_options)

    def get_result(self, execute_response: QPUExecuteResponse) -> QAMExecutionResult:
        """
        Retrieve results from execution on the QPU.
        """

        results = retrieve_results(
            job_id=execute_response.job_id,
            quantum_processor_id=self.quantum_processor_id,
            client=self._client_configuration,
            execution_options=execute_response.execution_options,
        )

        ro_sources = execute_response._executable.ro_sources
        decoded_buffers = {k: decode_buffer(v) for k, v in results.buffers.items()}

        result_memory = {}
        if len(decoded_buffers) != 0:
            extracted = _extract_memory_regions(
                execute_response._executable.memory_descriptors, ro_sources, decoded_buffers
            )
            for name, array in extracted.items():
                result_memory[name] = array
        elif not ro_sources:
            result_memory["ro"] = np.zeros((0, 0), dtype=np.int64)

        return QAMExecutionResult(
            executable=execute_response._executable,
            readout_data=result_memory,
            execution_duration_microseconds=results.execution_duration_microseconds,
        )
