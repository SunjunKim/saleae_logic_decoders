from typing import Any, Union, List
from saleae.analyzers import HighLevelAnalyzer, AnalyzerFrame, StringSetting, NumberSetting, ChoicesSetting
from saleae.data.timing import GraphTimeDelta, GraphTime
import enum
from datetime import datetime
import msp_const as const



# High level analyzers must subclass the HighLevelAnalyzer class.
class MspHla(HighLevelAnalyzer):
    BAUDRATE = 115200

    @enum.unique
    class State(enum.Enum):
        MSP_IDLE = enum.auto()
        MSP_START = enum.auto()
        MSP_HEADER_M = enum.auto()  # MSPv1
        MSP_HEADER_X = enum.auto()  # MSPv2

        MSP_FLAGS = enum.auto()
        MSP_PAYLOAD_SIZE = enum.auto()
        MSP_PAYLOAD_FUNC = enum.auto()
        MSP_PAYLOAD = enum.auto()
        MSP_CHECKSUM = enum.auto()

        MSPv2_FLAGS = enum.auto()
        MSPv2_FUNC_1 = enum.auto()
        MSPv2_FUNC_2 = enum.auto()
        MSPv2_SIZE_1 = enum.auto()
        MSPv2_SIZE_2 = enum.auto()
        MSPv2_PAYLOAD = enum.auto()
        MSPv2_CHECKSUM = enum.auto()

    result_types = {
        'info': {'format': '{{data.decoded}}'}
    }

    def __init__(self) -> None:
        ''' Initialize HLA. '''
        print("========== MSP ==========")
        self._state = self.State.MSP_IDLE
        self._bit_time = 1. / self.BAUDRATE
        self._time_to_clear = GraphTimeDelta(30. * self._bit_time)
        self._last_packet_time = GraphTime(datetime.utcfromtimestamp(0))

    def _clear(self) -> None:
        self._state = self.State.MSP_IDLE
        self._decode_func = None

    updated = False
    def _update_timeout(self, frame: AnalyzerFrame) -> None:
        if self.updated:
            return
        self.updated = True
        self._bit_time = ((frame.end_time - frame.start_time) / 9.5)
        # print(f"bit time: {self._bit_time}, baud {1./float(self._bit_time)}")
        self._time_to_clear = GraphTimeDelta(30. * float(self._bit_time))

    def _data_to_int(self, frame: AnalyzerFrame) -> int:
        return int.from_bytes(frame.data['data'], "little")

    def _calc_crc(self, data: int, crc: int, poly:int = const.MSP_CRC_POLY) -> int:
        crc ^= data
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ poly
            else:
                crc = crc << 1
        return crc & 0xFF

    def _calc_crc_xor(self, data: int, crc: int) -> int:
        return crc ^ data

    def _frame_info_get(self, frame, info, type="info"):
        return AnalyzerFrame(type, frame.start_time, frame.end_time, {'decoded': info})

    def _frame_info_with_times_get(self, start, end, info, type="info"):
        return AnalyzerFrame(type, start, end, {'decoded': info})

    def parse_func_end(self, state):
        self._state = state
        if state == self.State.MSP_IDLE:
            self._clear()

    def __get_value(self, data: List[int], cnt: int):
        value = 0
        for _ in range(cnt):
            value <<= 8
            value += data.pop(0)
        return value

    def parse_message_content(self, function, payload):
        result = []
        content = const.MSPv1_function_content_get(function)
        if not content:
            return [self._frame_info_with_times_get(
                    payload[0].start_time, payload[-1].end_time, "PAYLOAD")]
        for size, vars in content:
            value = 0
            start_time = payload[0].start_time
            if not size:
                continue
            if size < 0:
                # rest of the payload...
                end_time = payload[-1].end_time
                size = len(payload)
                if vars == str:
                    vars = "STR: "
                    for d in payload:
                        vars += chr(self._data_to_int(d))
                    vars += ""
            else:
                end_time = payload[size-1].end_time
                for idx in range(size):
                    value <<= 8
                    value += self._data_to_int(payload[idx])
            if type(vars) == str:
                value = vars
            elif type(vars) == dict:
                value = vars.get(value, "ERROR")
            elif type(vars) == list:
                value = vars[value]
            result.append(self._frame_info_with_times_get(start_time, end_time, value))

            payload = payload[size:]
            if not payload:
                break
        return result

    # -------------------------- MSP v1 parsing -------------------
    _crc_v1 = 0
    _payload_len = 0
    _payload_rcvd = 0
    _msp_v1_function = -1
    def parse_msp_v1(self, frame: AnalyzerFrame):
        result = []
        next_state = self.State.MSP_IDLE
        data = self._data_to_int(frame)

        if self._state == self.State.MSP_HEADER_M:
            _type = const.MSP_TYPE.get(data)
            if _type == "ELRS":
                next_state = self.State.MSP_FLAGS
            elif _type:
                next_state = self.State.MSP_PAYLOAD_SIZE
            else:
                _type = "INVALID"
            result = self._frame_info_get(frame, _type)
        elif self._state == self.State.MSP_FLAGS:
            # ELRS specific state, no CRC
            next_state = self.State.MSP_PAYLOAD_SIZE
            result = self._frame_info_get(frame, "Flags")
        elif self._state == self.State.MSP_PAYLOAD_SIZE:
            self._payload = []
            self._payload_rcvd = 0
            self._payload_len = data
            self._crc_v1 = self._calc_crc_xor(data, 0)
            next_state = self.State.MSP_PAYLOAD_FUNC
            result = self._frame_info_get(frame, "Length")
        elif self._state == self.State.MSP_PAYLOAD_FUNC:
            self._msp_v1_function = data
            self._crc_v1 = self._calc_crc_xor(data, self._crc_v1)
            next_state = self.State.MSP_PAYLOAD if self._payload_len else self.State.MSP_CHECKSUM
            func = const.MSPv1_function_get(data)
            result = self._frame_info_get(frame, f"Func: {func}")
            if func == "MSP_V2_FRAME":
                # Encapsulated V2 frame...
                next_state = self.State.MSPv2_FLAGS
        elif self._state == self.State.MSP_PAYLOAD:
            self._payload.append(frame)
            next_state = self.State.MSP_PAYLOAD
            self._crc_v1 = self._calc_crc_xor(data, self._crc_v1)
            # info = f"data[{self._payload_rcvd}]"
            #info = f"0x{data:02X},{data:>3d},{chr(data):>2s}"
            #result = [self._frame_info_get(frame, info)]
            self._payload_rcvd += 1
            if self._payload_len <= self._payload_rcvd:
                # TODO: Parse content of the known functions...
                result.extend(self.parse_message_content(self._msp_v1_function, self._payload))
                next_state = self.State.MSP_CHECKSUM
        elif self._state == self.State.MSP_CHECKSUM:
            info = ["CRC ERROR", "CRC"][data == self._crc_v1]
            result = self._frame_info_get(frame, info)
            next_state = self.State.MSP_IDLE
        # --------------------------------------------
        # Encapsulated V2 frame parsing
        else:
            self._crc_v1 = self._calc_crc_xor(data, self._crc_v1)
            result = self.parse_msp_v2(frame)
            self._payload_rcvd += 1
            if self._payload_len <= self._payload_rcvd:
                self._decode_func = self.parse_msp_v1
                next_state = self.State.MSP_CHECKSUM
            else:
                return result
        # --------------------------------------------

        self.parse_func_end(next_state)
        return result

    # -------------------------- MSP v2 parsing -------------------
    _crc_v2 = 0
    _payload_v2_len = 0
    _payload_v2_rcvd = 0
    msp_v2_func = (0, 0)
    msp_v2_size = (0, 0)
    def parse_msp_v2(self, frame: AnalyzerFrame):
        result = []
        next_state = self.State.MSP_IDLE
        data = self._data_to_int(frame)

        if self._state == self.State.MSP_HEADER_X:
            _type = const.MSP_TYPE.get(data, "INVALID")
            result = self._frame_info_get(frame, _type)
            next_state = self.State.MSPv2_FLAGS
        elif self._state == self.State.MSPv2_FLAGS:
            self._crc_v2 = self._calc_crc(data, 0)
            result = self._frame_info_get(frame, "Flags")
            next_state = self.State.MSPv2_FUNC_1
        elif self._state == self.State.MSPv2_FUNC_1:
            self._crc_v2 = self._calc_crc(data, self._crc_v2)
            self.msp_v2_func = (data << 8, frame.start_time)
            next_state = self.State.MSPv2_FUNC_2
        elif self._state == self.State.MSPv2_FUNC_2:  # uint16
            self._crc_v2 = self._calc_crc(data, self._crc_v2)
            func_id, start = self.msp_v2_func
            func_id += data
            func = const.MSPv2_function_get(func_id)
            frame.start_time = start
            result = self._frame_info_get(frame, f"Func: {func}")
            next_state = self.State.MSPv2_SIZE_1
        elif self._state == self.State.MSPv2_SIZE_1:
            self._crc_v2 = self._calc_crc(data, self._crc_v2)
            self.msp_v2_size = (data << 8, frame.start_time)
            next_state = self.State.MSPv2_SIZE_2
        elif self._state == self.State.MSPv2_SIZE_2:  # uint16
            self._crc_v2 = self._calc_crc(data, self._crc_v2)
            length, start = self.msp_v2_size
            length += data
            self._payload = []
            self._payload_v2_rcvd = 0
            self._payload_v2_len = length
            next_state = self.State.MSPv2_PAYLOAD if length else self.State.MSPv2_CHECKSUM
            frame.start_time = start
            result = self._frame_info_get(frame, f"Length {length}")
        elif self._state == self.State.MSPv2_PAYLOAD:
            self._payload.append(frame)
            self._crc_v2 = self._calc_crc(data, self._crc_v2)
            result = self._frame_info_get(frame, f"data[{self._payload_v2_rcvd}]")
            self._payload_v2_rcvd += 1
            next_state = self.State.MSPv2_PAYLOAD
            if self._payload_v2_len <= self._payload_v2_rcvd:
                # TODO: Parse content of the known functions...
                next_state = self.State.MSPv2_CHECKSUM
        elif self._state == self.State.MSPv2_CHECKSUM:
            info = ["CRC ERROR", "CRC"][data == self._crc_v2]
            result = self._frame_info_get(frame, info)
            next_state = self.State.MSP_IDLE

        self.parse_func_end(next_state)
        return result

    def parse_msp_message(self, frame: AnalyzerFrame):
        result = []
        data = self._data_to_int(frame)

        next_state = self.State.MSP_IDLE

        if self._state == self.State.MSP_IDLE:
            if data == const.MSP_START:
                self._crc_v1 = self._crc_v2 = 0
                next_state = self.State.MSP_START
                result = self._frame_info_get(frame, "Start")
        elif self._state == self.State.MSP_START:
            version = const.MSP_VERSION.get(data)
            if version == "V1":
                next_state = self.State.MSP_HEADER_M
                self._decode_func = self.parse_msp_v1
                result = self._frame_info_get(frame, "MSPv1")
            elif version == "V2":
                next_state = self.State.MSP_HEADER_X
                self._decode_func = self.parse_msp_v2
                result = self._frame_info_get(frame, "MSPv2")
        self.parse_func_end(next_state)
        return result

    def decode(self, frame: AnalyzerFrame) -> List[AnalyzerFrame]:
        if self._time_to_clear < (frame.start_time - self._last_packet_time):
            self._clear()
        # Update last received packet timing
        self._last_packet_time = frame.end_time
        # Calculate baudrate based on frame timing
        self._update_timeout(frame)

        if self._decode_func:
            return self._decode_func(frame)
        return self.parse_msp_message(frame)
