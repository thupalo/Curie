"""ctypes binding to the SAP NW RFC SDK 7.20/7.21, server side.

Why not pyrfc: pyrfc 3.x requires NW RFC SDK 7.50, whose server replies always
go out in Unicode (codepage 4103). A 4.5B backend cannot parse that and dumps
with CALL_FUNCTION_FORMAT_NOT_SUP in ab_RfcSetHeader. The 7.20 generation still
speaks the old single-byte protocol -- verified against TC1, which gets its
answers in codepage 1401 -- and supports backends down to 4.0B, where 7.50
stops at 4.6C. This module drives that older SDK directly, so no build
toolchain and no pyrfc rebuild is needed.

Both directions are bound: the server side (register at a gateway, install
callbacks, dispatch) and the client side (Client: open a connection, read a
function interface, invoke). One SDK generation per process is the rule, so
having the client here too is what lets Curie run as a single process
without pyrfc.

Types marshalled to/from Python:

    CHAR NUM STRING          str
    DATE                     datetime.date  (initial 00000000 -> None)
    TIME                     datetime.time  (initial 000000   -> None)
    INT INT1 INT2            int
    FLOAT                    float
    BCD DECF16 DECF34        decimal.Decimal
    BYTE XSTRING             bytes
    STRUCTURE                dict
    TABLE                    list of dict
"""
from __future__ import annotations

import ctypes as C
import datetime as dt
import logging
import os
import threading
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# Library loading
# --------------------------------------------------------------------------- #

log = logging.getLogger(__name__)

SDK_HOME = os.environ.get("SAPNWRFC720_HOME", r"C:\nwrfcsdk")


def _load() -> C.CDLL:
    """Load sapnwrfc.dll from the 7.20 SDK.

    The SDK locates its ICU dlls by scanning PATH, so its lib directory has to
    go in front: the 7.50 lib directory is usually on PATH already and carries
    a different ICU generation, which the loader would otherwise pick up and
    fail on ("Could not open the ICU common library").
    """
    lib_dir = os.path.join(SDK_HOME, "lib")
    if not os.path.isdir(lib_dir):
        raise RuntimeError(f"NW RFC SDK 7.20 not found at {SDK_HOME}")
    os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(lib_dir)
    return C.CDLL(os.path.join(lib_dir, "sapnwrfc.dll"))


lib = _load()

# --------------------------------------------------------------------------- #
# Enums and structures (sapnwrfc.h)
# --------------------------------------------------------------------------- #

RFC_OK = 0
RFC_COMMUNICATION_FAILURE = 1
RFC_LOGON_FAILURE = 2
RFC_ABAP_RUNTIME_FAILURE = 3
RFC_ABAP_MESSAGE = 4
RFC_ABAP_EXCEPTION = 5
RFC_CLOSED = 6
RFC_INVALID_HANDLE = 13
RFC_RETRY = 14
RFC_EXTERNAL_FAILURE = 15

# Return codes after which a client connection handle is no longer usable.
DEAD_CONNECTION_CODES = frozenset({
    RFC_COMMUNICATION_FAILURE, RFC_LOGON_FAILURE, RFC_ABAP_RUNTIME_FAILURE,
    RFC_ABAP_MESSAGE, RFC_CLOSED, RFC_INVALID_HANDLE,
})

RC_NAME = {
    0: "RFC_OK", 1: "RFC_COMMUNICATION_FAILURE", 2: "RFC_LOGON_FAILURE",
    3: "RFC_ABAP_RUNTIME_FAILURE", 4: "RFC_ABAP_MESSAGE", 5: "RFC_ABAP_EXCEPTION",
    6: "RFC_CLOSED", 8: "RFC_TIMEOUT", 14: "RFC_RETRY", 15: "RFC_EXTERNAL_FAILURE",
    17: "RFC_NOT_FOUND", 20: "RFC_INVALID_PARAMETER",
}

# RFCTYPE
CHAR, DATE, BCD, TIME, BYTE, TABLE, NUM, FLOAT, INT, INT2, INT1 = range(11)
STRUCTURE, DECF16, DECF34 = 17, 23, 24
STRING, XSTRING = 29, 30

# RFC_DIRECTION
IMPORT, EXPORT, CHANGING, TABLES = 0x01, 0x02, 0x03, 0x07


class RFC_ERROR_INFO(C.Structure):
    _fields_ = [
        ("code", C.c_int), ("group", C.c_int),
        ("key", C.c_wchar * 128), ("message", C.c_wchar * 512),
        ("abapMsgClass", C.c_wchar * 21), ("abapMsgType", C.c_wchar * 2),
        ("abapMsgNumber", C.c_wchar * 4),
        ("abapMsgV1", C.c_wchar * 51), ("abapMsgV2", C.c_wchar * 51),
        ("abapMsgV3", C.c_wchar * 51), ("abapMsgV4", C.c_wchar * 51),
    ]


class RFC_CONNECTION_PARAMETER(C.Structure):
    _fields_ = [("name", C.c_wchar_p), ("value", C.c_wchar_p)]


class RFC_PARAMETER_DESC(C.Structure):
    _fields_ = [
        ("name", C.c_wchar * 31), ("type", C.c_int), ("direction", C.c_int),
        ("nucLength", C.c_uint), ("ucLength", C.c_uint), ("decimals", C.c_uint),
        ("typeDescHandle", C.c_void_p),
        ("defaultValue", C.c_wchar * 31), ("parameterText", C.c_wchar * 80),
        ("optional", C.c_ubyte), ("extendedDescription", C.c_void_p),
    ]


class RFC_FIELD_DESC(C.Structure):
    _fields_ = [
        ("name", C.c_wchar * 31), ("type", C.c_int),
        ("nucLength", C.c_uint), ("nucOffset", C.c_uint),
        ("ucLength", C.c_uint), ("ucOffset", C.c_uint),
        ("decimals", C.c_uint), ("typeDescHandle", C.c_void_p),
        ("extendedDescription", C.c_void_p),
    ]


class RFC_ATTRIBUTES(C.Structure):
    _fields_ = [
        ("dest", C.c_wchar * 65), ("host", C.c_wchar * 101),
        ("partnerHost", C.c_wchar * 101), ("sysNumber", C.c_wchar * 3),
        ("sysId", C.c_wchar * 9), ("client", C.c_wchar * 4),
        ("user", C.c_wchar * 13), ("language", C.c_wchar * 3),
        ("trace", C.c_wchar * 2), ("isoLanguage", C.c_wchar * 3),
        ("codepage", C.c_wchar * 5), ("partnerCodepage", C.c_wchar * 5),
        ("rfcRole", C.c_wchar * 2), ("type", C.c_wchar * 2),
        ("partnerType", C.c_wchar * 2), ("rel", C.c_wchar * 5),
        ("partnerRel", C.c_wchar * 5), ("kernelRel", C.c_wchar * 5),
        ("cpicConvId", C.c_wchar * 9), ("progName", C.c_wchar * 129),
        ("partnerBytesPerChar", C.c_wchar * 2),
        ("partnerSystemCodepage", C.c_wchar * 5), ("reserved", C.c_wchar * 79),
    ]


SERVER_FUNCTION = C.CFUNCTYPE(
    C.c_int, C.c_void_p, C.c_void_p, C.POINTER(RFC_ERROR_INFO)
)

_H = C.c_void_p
_E = C.POINTER(RFC_ERROR_INFO)

_PROTOTYPES = (
    ("RfcGetVersion", C.c_wchar_p, [C.POINTER(C.c_uint)] * 3),
    ("RfcOpenConnection", _H, [C.c_void_p, C.c_uint, _E]),
    ("RfcCloseConnection", C.c_int, [_H, _E]),
    ("RfcRegisterServer", _H, [C.c_void_p, C.c_uint, _E]),
    ("RfcListenAndDispatch", C.c_int, [_H, C.c_int, _E]),
    ("RfcInstallServerFunction", C.c_int, [C.c_wchar_p, _H, SERVER_FUNCTION, _E]),
    ("RfcGetFunctionDesc", _H, [_H, C.c_wchar_p, _E]),
    ("RfcGetFunctionName", C.c_int, [_H, C.c_void_p, _E]),
    ("RfcGetConnectionAttributes", C.c_int, [_H, C.POINTER(RFC_ATTRIBUTES), _E]),
    ("RfcGetParameterCount", C.c_int, [_H, C.POINTER(C.c_uint), _E]),
    ("RfcGetParameterDescByIndex", C.c_int,
     [_H, C.c_uint, C.POINTER(RFC_PARAMETER_DESC), _E]),
    ("RfcGetFieldCount", C.c_int, [_H, C.POINTER(C.c_uint), _E]),
    ("RfcGetFieldDescByIndex", C.c_int,
     [_H, C.c_uint, C.POINTER(RFC_FIELD_DESC), _E]),
    ("RfcGetChars", C.c_int, [_H, C.c_wchar_p, C.c_void_p, C.c_uint, _E]),
    ("RfcSetChars", C.c_int, [_H, C.c_wchar_p, C.c_wchar_p, C.c_uint, _E]),
    ("RfcGetNum", C.c_int, [_H, C.c_wchar_p, C.c_void_p, C.c_uint, _E]),
    ("RfcSetNum", C.c_int, [_H, C.c_wchar_p, C.c_wchar_p, C.c_uint, _E]),
    ("RfcGetStringLength", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_uint), _E]),
    ("RfcGetString", C.c_int,
     [_H, C.c_wchar_p, C.c_void_p, C.c_uint, C.POINTER(C.c_uint), _E]),
    ("RfcSetString", C.c_int, [_H, C.c_wchar_p, C.c_wchar_p, C.c_uint, _E]),
    ("RfcGetDate", C.c_int, [_H, C.c_wchar_p, C.c_void_p, _E]),
    ("RfcSetDate", C.c_int, [_H, C.c_wchar_p, C.c_void_p, _E]),
    ("RfcGetTime", C.c_int, [_H, C.c_wchar_p, C.c_void_p, _E]),
    ("RfcSetTime", C.c_int, [_H, C.c_wchar_p, C.c_void_p, _E]),
    ("RfcGetInt", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_int), _E]),
    ("RfcSetInt", C.c_int, [_H, C.c_wchar_p, C.c_int, _E]),
    ("RfcGetInt1", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_ubyte), _E]),
    ("RfcSetInt1", C.c_int, [_H, C.c_wchar_p, C.c_ubyte, _E]),
    ("RfcGetInt2", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_short), _E]),
    ("RfcSetInt2", C.c_int, [_H, C.c_wchar_p, C.c_short, _E]),
    ("RfcGetFloat", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_double), _E]),
    ("RfcSetFloat", C.c_int, [_H, C.c_wchar_p, C.c_double, _E]),
    ("RfcGetBytes", C.c_int, [_H, C.c_wchar_p, C.c_void_p, C.c_uint, _E]),
    ("RfcSetBytes", C.c_int, [_H, C.c_wchar_p, C.c_void_p, C.c_uint, _E]),
    ("RfcGetXString", C.c_int,
     [_H, C.c_wchar_p, C.c_void_p, C.c_uint, C.POINTER(C.c_uint), _E]),
    ("RfcSetXString", C.c_int, [_H, C.c_wchar_p, C.c_void_p, C.c_uint, _E]),
    ("RfcGetStructure", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_void_p), _E]),
    ("RfcGetTable", C.c_int, [_H, C.c_wchar_p, C.POINTER(C.c_void_p), _E]),
    ("RfcGetRowCount", C.c_int, [_H, C.POINTER(C.c_uint), _E]),
    ("RfcMoveToFirstRow", C.c_int, [_H, _E]),
    ("RfcMoveToNextRow", C.c_int, [_H, _E]),
    ("RfcGetCurrentRow", _H, [_H, _E]),
    ("RfcAppendNewRow", _H, [_H, _E]),
    ("RfcDeleteAllRows", C.c_int, [_H, _E]),
    # client direction
    ("RfcCreateFunction", _H, [_H, _E]),
    ("RfcDestroyFunction", C.c_int, [_H, _E]),
    ("RfcInvoke", C.c_int, [_H, _H, _E]),
    ("RfcPing", C.c_int, [_H, _E]),
    ("RfcIsConnectionHandleValid", C.c_int, [_H, C.POINTER(C.c_int), _E]),
)

for _name, _restype, _argtypes in _PROTOTYPES:
    _fn = getattr(lib, _name)
    _fn.restype, _fn.argtypes = _restype, _argtypes


class RFCError(Exception):
    """An SDK call failed; carries the RFC_ERROR_INFO the SDK filled in."""

    def __init__(self, where: str, err: RFC_ERROR_INFO):
        self.code, self.key, self.message = err.code, err.key, err.message
        super().__init__(
            f"{where}: {RC_NAME.get(err.code, err.code)} "
            f"{err.key} {err.message}".rstrip()
        )


class ABAPException(Exception):
    """An ABAP exception, in either direction.

    Raised in a handler to return an exception to the ABAP caller: the name
    has to be declared in the function module's EXCEPTIONS list, and ABAP
    sees it as the corresponding non-zero SY-SUBRC.

    Raised by Client.call() when the called function module raised one;
    `key` is the exception name and `message` whatever text came with it.
    str() is the key alone, which is what the server trampoline sends back.
    """

    def __init__(self, key: str, message: str = ""):
        super().__init__(key)
        self.key = key
        self.message = message


def _check(rc: int, where: str, err: RFC_ERROR_INFO) -> None:
    if rc != RFC_OK:
        raise RFCError(where, err)


def _conn_params(mapping: dict):
    arr = (RFC_CONNECTION_PARAMETER * len(mapping))()
    for i, (key, value) in enumerate(mapping.items()):
        arr[i].name, arr[i].value = str(key), str(value)
    return arr, len(mapping)


def sdk_version() -> str:
    major, minor, patch = C.c_uint(), C.c_uint(), C.c_uint()
    return lib.RfcGetVersion(major, minor, patch)


def parameter_descs(func_desc) -> list:
    """The RFC_PARAMETER_DESC of every parameter of a function description."""
    err = RFC_ERROR_INFO()
    count = C.c_uint()
    _check(lib.RfcGetParameterCount(func_desc, C.byref(count), C.byref(err)),
           "RfcGetParameterCount", err)
    params = []
    for i in range(count.value):
        pdesc = RFC_PARAMETER_DESC()
        _check(
            lib.RfcGetParameterDescByIndex(func_desc, i, C.byref(pdesc), C.byref(err)),
            "RfcGetParameterDescByIndex", err,
        )
        params.append(pdesc)
    return params


DIRECTION_NAME = {IMPORT: "IMPORT", EXPORT: "EXPORT", CHANGING: "CHANGING",
                  TABLES: "TABLES"}
TYPE_NAME = {
    CHAR: "CHAR", DATE: "DATE", BCD: "BCD", TIME: "TIME", BYTE: "BYTE",
    TABLE: "TABLE", NUM: "NUM", FLOAT: "FLOAT", INT: "INT", INT2: "INT2",
    INT1: "INT1", STRUCTURE: "STRUCTURE", DECF16: "DECF16", DECF34: "DECF34",
    STRING: "STRING", XSTRING: "XSTRING",
}


def describe_parameter(p: RFC_PARAMETER_DESC) -> dict:
    """A plain-dict view of a parameter descriptor, for callers and logs."""
    return {
        "name": p.name,
        "direction": DIRECTION_NAME.get(p.direction, str(p.direction)),
        "type": TYPE_NAME.get(p.type, str(p.type)),
        "length": p.nucLength,
        "decimals": p.decimals,
        "optional": bool(p.optional),
        "default": p.defaultValue,
        "text": p.parameterText,
    }


# --------------------------------------------------------------------------- #
# Reading values out of a data container (function handle, structure, row)
# --------------------------------------------------------------------------- #


def _get_string(handle, name: str, err) -> str:
    length = C.c_uint()
    _check(lib.RfcGetStringLength(handle, name, C.byref(length), C.byref(err)),
           f"RfcGetStringLength({name})", err)
    size = length.value + 1
    buf = (C.c_wchar * size)()
    written = C.c_uint()
    _check(lib.RfcGetString(handle, name, buf, size, C.byref(written), C.byref(err)),
           f"RfcGetString({name})", err)
    return buf[:written.value]


def _get_chars(handle, name: str, ulen: int, err) -> str:
    # ucLength counts bytes; two per character in the SDK's UTF-16 container.
    count = max(ulen // 2, 1)
    buf = (C.c_wchar * (count + 1))()
    _check(lib.RfcGetChars(handle, name, buf, count, C.byref(err)),
           f"RfcGetChars({name})", err)
    return buf[:count]


def _get_xstring(handle, name: str, err) -> bytes:
    length = C.c_uint()
    _check(lib.RfcGetStringLength(handle, name, C.byref(length), C.byref(err)),
           f"RfcGetStringLength({name})", err)
    size = max(length.value, 1)
    buf = (C.c_ubyte * size)()
    written = C.c_uint()
    _check(lib.RfcGetXString(handle, name, buf, size, C.byref(written), C.byref(err)),
           f"RfcGetXString({name})", err)
    return bytes(buf[:written.value])


def _to_decimal(text: str):
    text = text.strip()
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return text


def get_value(handle, name: str, rfctype: int, ulen: int, type_desc, err):
    """Read one parameter/field out of a data container as a Python value."""
    if rfctype in (CHAR, NUM):
        text = (_get_num(handle, name, ulen, err) if rfctype == NUM
                else _get_chars(handle, name, ulen, err))
        return text.rstrip()
    if rfctype == STRING:
        return _get_string(handle, name, err)
    if rfctype in (BCD, DECF16, DECF34):
        return _to_decimal(_get_string(handle, name, err))
    if rfctype == DATE:
        buf = (C.c_wchar * 9)()
        _check(lib.RfcGetDate(handle, name, buf, C.byref(err)),
               f"RfcGetDate({name})", err)
        text = buf[:8]
        if not text.strip() or text == "00000000":
            return None
        try:
            return dt.datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return text
    if rfctype == TIME:
        buf = (C.c_wchar * 7)()
        _check(lib.RfcGetTime(handle, name, buf, C.byref(err)),
               f"RfcGetTime({name})", err)
        text = buf[:6]
        if not text.strip():
            return None
        try:
            return dt.datetime.strptime(text, "%H%M%S").time()
        except ValueError:
            return text
    if rfctype == INT:
        value = C.c_int()
        _check(lib.RfcGetInt(handle, name, C.byref(value), C.byref(err)),
               f"RfcGetInt({name})", err)
        return value.value
    if rfctype == INT1:
        value = C.c_ubyte()
        _check(lib.RfcGetInt1(handle, name, C.byref(value), C.byref(err)),
               f"RfcGetInt1({name})", err)
        return value.value
    if rfctype == INT2:
        value = C.c_short()
        _check(lib.RfcGetInt2(handle, name, C.byref(value), C.byref(err)),
               f"RfcGetInt2({name})", err)
        return value.value
    if rfctype == FLOAT:
        value = C.c_double()
        _check(lib.RfcGetFloat(handle, name, C.byref(value), C.byref(err)),
               f"RfcGetFloat({name})", err)
        return value.value
    if rfctype == BYTE:
        size = max(ulen, 1)
        buf = (C.c_ubyte * size)()
        _check(lib.RfcGetBytes(handle, name, buf, size, C.byref(err)),
               f"RfcGetBytes({name})", err)
        return bytes(buf)
    if rfctype == XSTRING:
        return _get_xstring(handle, name, err)
    if rfctype == STRUCTURE:
        sub = C.c_void_p()
        _check(lib.RfcGetStructure(handle, name, C.byref(sub), C.byref(err)),
               f"RfcGetStructure({name})", err)
        return read_structure(sub, type_desc, err)
    if rfctype == TABLE:
        table = C.c_void_p()
        _check(lib.RfcGetTable(handle, name, C.byref(table), C.byref(err)),
               f"RfcGetTable({name})", err)
        return read_table(table, type_desc, err)
    raise RuntimeError(f"parameter {name}: RFCTYPE {rfctype} not supported")


def _get_num(handle, name: str, ulen: int, err) -> str:
    count = max(ulen // 2, 1)
    buf = (C.c_wchar * (count + 1))()
    _check(lib.RfcGetNum(handle, name, buf, count, C.byref(err)),
           f"RfcGetNum({name})", err)
    return buf[:count]


def fields_of(type_desc, err) -> list:
    """Field descriptors of a structure/table line type."""
    count = C.c_uint()
    _check(lib.RfcGetFieldCount(type_desc, C.byref(count), C.byref(err)),
           "RfcGetFieldCount", err)
    out = []
    for i in range(count.value):
        desc = RFC_FIELD_DESC()
        _check(lib.RfcGetFieldDescByIndex(type_desc, i, C.byref(desc), C.byref(err)),
               "RfcGetFieldDescByIndex", err)
        out.append(desc)
    return out


def read_structure(handle, type_desc, err) -> dict:
    return {
        f.name: get_value(handle, f.name, f.type, f.ucLength, f.typeDescHandle, err)
        for f in fields_of(type_desc, err)
    }


def read_table(table, type_desc, err) -> list:
    rows = C.c_uint()
    _check(lib.RfcGetRowCount(table, C.byref(rows), C.byref(err)),
           "RfcGetRowCount", err)
    if not rows.value:
        return []
    fields = fields_of(type_desc, err)
    _check(lib.RfcMoveToFirstRow(table, C.byref(err)), "RfcMoveToFirstRow", err)
    out = []
    for i in range(rows.value):
        if i:
            _check(lib.RfcMoveToNextRow(table, C.byref(err)), "RfcMoveToNextRow", err)
        row = lib.RfcGetCurrentRow(table, C.byref(err))
        if not row:
            raise RFCError("RfcGetCurrentRow", err)
        out.append({
            f.name: get_value(row, f.name, f.type, f.ucLength, f.typeDescHandle, err)
            for f in fields
        })
    return out


# --------------------------------------------------------------------------- #
# Writing values back into a data container
# --------------------------------------------------------------------------- #


def set_value(handle, name: str, rfctype: int, type_desc, value, err,
              ulen: int = 0) -> None:
    """Write one Python value into a data container.

    ulen is the field's ucLength (bytes, two per character); when given, CHAR
    and NUM values are cut to the field length instead of failing the call.
    """
    if value is None:
        return
    if rfctype in (CHAR, NUM):
        text = str(value)
        if ulen:
            text = text[:max(ulen // 2, 1)]
        setter = lib.RfcSetNum if rfctype == NUM else lib.RfcSetChars
        _check(setter(handle, name, text, len(text), C.byref(err)),
               f"set({name})", err)
        return
    if rfctype == STRING:
        text = str(value)
        _check(lib.RfcSetString(handle, name, text, len(text), C.byref(err)),
               f"RfcSetString({name})", err)
        return
    if rfctype in (BCD, DECF16, DECF34):
        text = str(value)
        _check(lib.RfcSetString(handle, name, text, len(text), C.byref(err)),
               f"RfcSetString({name})", err)
        return
    if rfctype == DATE:
        text = value.strftime("%Y%m%d") if isinstance(value, (dt.date, dt.datetime)) \
            else str(value)
        buf = (C.c_wchar * 8)()
        buf[:8] = text[:8].ljust(8)
        _check(lib.RfcSetDate(handle, name, buf, C.byref(err)),
               f"RfcSetDate({name})", err)
        return
    if rfctype == TIME:
        text = value.strftime("%H%M%S") if isinstance(value, (dt.time, dt.datetime)) \
            else str(value)
        buf = (C.c_wchar * 6)()
        buf[:6] = text[:6].ljust(6)
        _check(lib.RfcSetTime(handle, name, buf, C.byref(err)),
               f"RfcSetTime({name})", err)
        return
    if rfctype == INT:
        _check(lib.RfcSetInt(handle, name, int(value), C.byref(err)),
               f"RfcSetInt({name})", err)
        return
    if rfctype == INT1:
        _check(lib.RfcSetInt1(handle, name, int(value), C.byref(err)),
               f"RfcSetInt1({name})", err)
        return
    if rfctype == INT2:
        _check(lib.RfcSetInt2(handle, name, int(value), C.byref(err)),
               f"RfcSetInt2({name})", err)
        return
    if rfctype == FLOAT:
        _check(lib.RfcSetFloat(handle, name, float(value), C.byref(err)),
               f"RfcSetFloat({name})", err)
        return
    if rfctype in (BYTE, XSTRING):
        raw = bytes(value)
        buf = (C.c_ubyte * max(len(raw), 1)).from_buffer_copy(raw.ljust(1, b"\x00"))
        setter = lib.RfcSetBytes if rfctype == BYTE else lib.RfcSetXString
        _check(setter(handle, name, buf, len(raw), C.byref(err)),
               f"set({name})", err)
        return
    if rfctype == STRUCTURE:
        sub = C.c_void_p()
        _check(lib.RfcGetStructure(handle, name, C.byref(sub), C.byref(err)),
               f"RfcGetStructure({name})", err)
        write_structure(sub, type_desc, value, err)
        return
    if rfctype == TABLE:
        table = C.c_void_p()
        _check(lib.RfcGetTable(handle, name, C.byref(table), C.byref(err)),
               f"RfcGetTable({name})", err)
        write_table(table, type_desc, value, err)
        return
    raise RuntimeError(f"parameter {name}: RFCTYPE {rfctype} not supported")


def write_structure(handle, type_desc, values: dict, err) -> None:
    by_name = {f.name: f for f in fields_of(type_desc, err)}
    for key, value in values.items():
        field = by_name.get(key.upper())
        if field is None and len(by_name) == 1 and len(values) == 1:
            # A flat table line (TABLES t LIKE zai_line, a CHAR72) shows up
            # as a one-field structure named after its type; whatever the
            # caller called the value, it is that field.
            field = next(iter(by_name.values()))
        if field is None:
            raise RuntimeError(f"no field {key} in structure")
        set_value(handle, field.name, field.type, field.typeDescHandle, value, err,
                  ulen=field.ucLength)


def write_table(table, type_desc, rows, err) -> None:
    # Replace the contents: ABAP TABLES parameters arrive filled by the caller.
    _check(lib.RfcDeleteAllRows(table, C.byref(err)), "RfcDeleteAllRows", err)
    for values in rows:
        row = lib.RfcAppendNewRow(table, C.byref(err))
        if not row:
            raise RFCError("RfcAppendNewRow", err)
        write_structure(row, type_desc, values, err)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class Server:
    """A registered RFC server program.

    server_params needs PROGRAM_ID plus GWHOST/GWSERV; client_params are the
    logon parameters of the backend, used to read the interfaces of the
    function modules being served.

    reg_count is how many times to register at the gateway. Each registration
    is one conversation with the backend and handles one call at a time, so
    this is what decides how many ABAP calls can run in parallel -- the 7.20
    SDK has no REG_COUNT parameter of its own (that is a 7.50 RfcCreateServer
    feature), so start() opens the registrations and drives each in a thread.
    With reg_count > 1 the handlers run concurrently: keep them thread-safe.
    """

    def __init__(self, server_params: dict, client_params: dict,
                 reg_count: int = 1, dispatch_timeout: int = 5):
        self.server_params = dict(server_params)
        self.client_params = dict(client_params)
        self.reg_count = max(1, int(reg_count))
        # Bounds how long stop() waits: a worker only notices the stop flag
        # between dispatch calls.
        self.dispatch_timeout = dispatch_timeout
        self._handles: list = []
        self._threads: list = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conn = None
        # The SDK keeps raw pointers to the callbacks, so they must stay
        # referenced here or Python would collect them mid-flight.
        self._callbacks: dict = {}
        self._params: dict = {}
        self.functions: dict = {}

    # -- connection used for metadata ------------------------------------- #

    def _client(self):
        if self._conn is None:
            err = RFC_ERROR_INFO()
            arr, count = _conn_params(self.client_params)
            handle = lib.RfcOpenConnection(arr, count, C.byref(err))
            if not handle:
                raise RFCError("RfcOpenConnection", err)
            self._conn = handle
        return self._conn

    def function_desc(self, func_name: str):
        err = RFC_ERROR_INFO()
        desc = lib.RfcGetFunctionDesc(
            C.c_void_p(self._client()), func_name, C.byref(err)
        )
        if not desc:
            raise RFCError(f"RfcGetFunctionDesc({func_name})", err)
        return desc

    # -- installing handlers ---------------------------------------------- #

    def add_function(self, func_name: str, callback) -> None:
        """Install a Python callback under the name of an ABAP function module."""
        func_name = func_name.upper()
        if func_name in self._callbacks:
            raise ValueError(f"handler for {func_name} already installed")
        desc = self.function_desc(func_name)
        err = RFC_ERROR_INFO()
        self._params[func_name] = parameter_descs(desc)

        trampoline = SERVER_FUNCTION(self._make_trampoline(func_name, callback))
        _check(
            lib.RfcInstallServerFunction(None, desc, trampoline, C.byref(err)),
            f"RfcInstallServerFunction({func_name})", err,
        )
        self._callbacks[func_name] = trampoline
        self.functions[func_name] = callback

    def _make_trampoline(self, func_name: str, callback):
        """Wrap a Python callback in the C signature the SDK calls."""

        def trampoline(rfc_handle, func_handle, error_info):
            err = RFC_ERROR_INFO()
            try:
                kwargs = {}
                for p in self._params[func_name]:
                    if p.direction in (IMPORT, CHANGING, TABLES):
                        kwargs[p.name] = get_value(
                            C.c_void_p(func_handle), p.name, p.type,
                            p.ucLength, p.typeDescHandle, err,
                        )
                context = {
                    "function_name": func_name,
                    "connection_attributes": connection_attributes(rfc_handle),
                }
                result = callback(context, **kwargs) or {}

                by_name = {p.name: p for p in self._params[func_name]}
                for key, value in result.items():
                    p = by_name.get(key.upper())
                    if p is None:
                        raise RuntimeError(
                            f"{func_name}: no parameter {key} in the interface"
                        )
                    if p.direction == IMPORT:
                        raise RuntimeError(
                            f"{func_name}: {key} is an IMPORT parameter, "
                            "it cannot be returned"
                        )
                    set_value(C.c_void_p(func_handle), p.name, p.type,
                              p.typeDescHandle, value, err, ulen=p.ucLength)
                return RFC_OK
            except ABAPException as exc:
                error_info[0].code = RFC_ABAP_EXCEPTION
                error_info[0].key = str(exc)[:127]
                error_info[0].message = str(exc)[:511]
                return RFC_ABAP_EXCEPTION
            except BaseException as exc:  # noqa: BLE001 -- must not cross into C
                self.on_error(func_name, exc)
                error_info[0].code = RFC_EXTERNAL_FAILURE
                error_info[0].key = type(exc).__name__[:127]
                error_info[0].message = f"{type(exc).__name__}: {exc}"[:511]
                return RFC_EXTERNAL_FAILURE

        return trampoline

    def on_error(self, func_name: str, exc: BaseException) -> None:
        """Called when a handler raises; override or replace to log."""

    # -- lifecycle --------------------------------------------------------- #

    def register(self):
        """Open one registration at the gateway and return its handle."""
        err = RFC_ERROR_INFO()
        arr, count = _conn_params(self.server_params)
        handle = lib.RfcRegisterServer(arr, count, C.byref(err))
        if not handle:
            raise RFCError("RfcRegisterServer", err)
        with self._lock:
            self._handles.append(handle)
        return handle

    def dispatch(self, handle, timeout: int = 30) -> int:
        """Wait for one request on this registration and process it.

        RFC_RETRY means the timeout elapsed with no request -- the normal idle
        case. RFC_CLOSED means the backend hung up and the registration has to
        be opened again.
        """
        err = RFC_ERROR_INFO()
        rc = lib.RfcListenAndDispatch(C.c_void_p(handle), timeout, C.byref(err))
        if rc in (RFC_ABAP_EXCEPTION, RFC_EXTERNAL_FAILURE):
            # The handler's own verdict, already delivered to the caller; the
            # registration is still good. Should the SDK have dropped it
            # after all, the next dispatch says RFC_CLOSED and the worker
            # re-registers then.
            log.debug("dispatch: handler returned %s %s", RC_NAME.get(rc, rc), err.key)
            return RFC_OK
        if rc not in (RFC_OK, RFC_RETRY, RFC_CLOSED):
            raise RFCError("RfcListenAndDispatch", err)
        return rc

    def _drop(self, handle) -> None:
        err = RFC_ERROR_INFO()
        with self._lock:
            if handle in self._handles:
                self._handles.remove(handle)
        lib.RfcCloseConnection(C.c_void_p(handle), C.byref(err))

    def _worker(self, index: int) -> None:
        """Serve one registration until stop() is called."""
        handle = self.register()
        log.debug("registration %d open", index)
        while not self._stop.is_set():
            try:
                rc = self.dispatch(handle, self.dispatch_timeout)
            except RFCError as exc:
                if self._stop.is_set():
                    break
                log.error("registration %d: %s", index, exc)
                rc = RFC_CLOSED
            if rc == RFC_CLOSED and not self._stop.is_set():
                # Backend hung up (system restart, gateway bounce): the
                # registration is spent, open a fresh one in its place.
                log.warning("registration %d closed, re-registering", index)
                self._drop(handle)
                while not self._stop.is_set():
                    try:
                        handle = self.register()
                        break
                    except RFCError as exc:
                        log.error("registration %d failed: %s", index, exc)
                        self._stop.wait(self.dispatch_timeout)
        self._drop(handle)
        log.debug("registration %d closed", index)

    def start(self) -> None:
        """Open reg_count registrations, each served by its own thread."""
        self._stop.clear()
        for index in range(self.reg_count):
            thread = threading.Thread(
                target=self._worker, args=(index,),
                name=f"rfc-server-{index}", daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Ask the workers to finish and wait for them.

        A worker checks the flag only between dispatch calls, so this takes up
        to dispatch_timeout seconds. In-flight calls are always run to
        completion -- the ABAP caller never sees a half-served request.
        """
        self._stop.set()
        deadline = self.dispatch_timeout + 5 if timeout is None else timeout
        for thread in self._threads:
            thread.join(deadline)
        self._threads.clear()

    @property
    def registrations(self) -> int:
        with self._lock:
            return len(self._handles)

    def close(self) -> None:
        """Release everything. Call stop() first if the workers are running."""
        err = RFC_ERROR_INFO()
        with self._lock:
            handles, self._handles = self._handles, []
        for handle in handles:
            lib.RfcCloseConnection(C.c_void_p(handle), C.byref(err))
        if self._conn is not None:
            lib.RfcCloseConnection(C.c_void_p(self._conn), C.byref(err))
            self._conn = None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class Client:
    """One client connection to the backend: Python calls ABAP.

    The same 7.20 SDK the server uses, so a process needs only one SDK.
    Opened lazily; a handle that the SDK reports dead (backend hung up, short
    dump in the called function, logon failure) is dropped and the next call
    opens a new one. A handle must be used by one thread at a time -- see
    rfc.client.SapClient for the pool that enforces it.
    """

    def __init__(self, params: dict):
        self.params = dict(params)
        self._handle = None
        self._descs: dict = {}
        self._params: dict = {}

    # -- lifecycle -------------------------------------------------------- #

    def open(self):
        if self._handle is None:
            err = RFC_ERROR_INFO()
            arr, count = _conn_params(self.params)
            handle = lib.RfcOpenConnection(arr, count, C.byref(err))
            if not handle:
                raise RFCError("RfcOpenConnection", err)
            self._handle = handle
        return self._handle

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def close(self) -> None:
        if self._handle is not None:
            err = RFC_ERROR_INFO()
            lib.RfcCloseConnection(C.c_void_p(self._handle), C.byref(err))
            self._handle = None
        # Function descriptions belong to the SDK repository, not the
        # connection, but a handle from a dead repository is not worth keeping.
        self._descs.clear()
        self._params.clear()

    def _drop(self) -> None:
        """Forget a handle the SDK has declared unusable."""
        self.close()

    def is_valid(self) -> bool:
        if self._handle is None:
            return False
        err = RFC_ERROR_INFO()
        valid = C.c_int()
        rc = lib.RfcIsConnectionHandleValid(
            C.c_void_p(self._handle), C.byref(valid), C.byref(err))
        return rc == RFC_OK and bool(valid.value)

    def ping(self) -> None:
        err = RFC_ERROR_INFO()
        rc = lib.RfcPing(C.c_void_p(self.open()), C.byref(err))
        if rc != RFC_OK:
            self._drop()
            raise RFCError("RfcPing", err)

    def attributes(self) -> dict:
        return connection_attributes(self.open())

    # -- metadata ---------------------------------------------------------- #

    def function_desc(self, func_name: str):
        func_name = func_name.upper()
        desc = self._descs.get(func_name)
        if desc is None:
            err = RFC_ERROR_INFO()
            desc = lib.RfcGetFunctionDesc(
                C.c_void_p(self.open()), func_name, C.byref(err))
            if not desc:
                if err.code in DEAD_CONNECTION_CODES:
                    self._drop()
                raise RFCError(f"RfcGetFunctionDesc({func_name})", err)
            self._descs[func_name] = desc
        return desc

    def parameters(self, func_name: str) -> list:
        """RFC_PARAMETER_DESC list of a function module (cached)."""
        func_name = func_name.upper()
        params = self._params.get(func_name)
        if params is None:
            params = parameter_descs(self.function_desc(func_name))
            self._params[func_name] = params
        return params

    def interface(self, func_name: str) -> list:
        """The interface as plain dicts (see describe_parameter)."""
        return [describe_parameter(p) for p in self.parameters(func_name)]

    # -- calls ------------------------------------------------------------- #

    def call(self, func_name: str, **params) -> dict:
        """Call a function module; returns EXPORT, CHANGING and TABLES values.

        Keyword names are the ABAP parameter names (case-insensitive). Values
        follow the type table in the module docstring; tables are lists of
        dicts. Unknown parameters raise ValueError before anything is sent.
        An exception raised by the function module arrives as ABAPException;
        a short dump or a lost connection as RFCError, after which the handle
        is dropped.
        """
        func_name = func_name.upper()
        plist = self.parameters(func_name)
        by_name = {p.name: p for p in plist}
        for key in params:
            if key.upper() not in by_name:
                raise ValueError(f"{func_name}: no parameter {key} in the interface")

        err = RFC_ERROR_INFO()
        handle = self.open()
        func = lib.RfcCreateFunction(self.function_desc(func_name), C.byref(err))
        if not func:
            raise RFCError(f"RfcCreateFunction({func_name})", err)
        try:
            for key, value in params.items():
                p = by_name[key.upper()]
                set_value(C.c_void_p(func), p.name, p.type, p.typeDescHandle,
                          value, err, ulen=p.ucLength)
            rc = lib.RfcInvoke(C.c_void_p(handle), C.c_void_p(func), C.byref(err))
            if rc == RFC_ABAP_EXCEPTION:
                raise ABAPException(err.key, err.message)
            if rc != RFC_OK:
                if rc in DEAD_CONNECTION_CODES:
                    self._drop()
                raise RFCError(f"RfcInvoke({func_name})", err)
            out = {}
            for p in plist:
                if p.direction in (EXPORT, CHANGING, TABLES):
                    out[p.name] = get_value(C.c_void_p(func), p.name, p.type,
                                            p.ucLength, p.typeDescHandle, err)
            return out
        finally:
            cleanup = RFC_ERROR_INFO()
            lib.RfcDestroyFunction(C.c_void_p(func), C.byref(cleanup))


def connection_attributes(handle) -> dict:
    """Who is calling: sysId, client, user, progName and so on."""
    err = RFC_ERROR_INFO()
    attr = RFC_ATTRIBUTES()
    rc = lib.RfcGetConnectionAttributes(C.c_void_p(handle), C.byref(attr), C.byref(err))
    if rc != RFC_OK:
        return {}
    return {
        name: getattr(attr, name)
        for name, _ in RFC_ATTRIBUTES._fields_
        if name != "reserved" and getattr(attr, name)
    }
