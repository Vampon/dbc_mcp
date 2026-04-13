"""
DBC文件统一操作插件
支持DBC文件的查询、新增、删除、修改、校验等所有操作
"""
import cantools.database
from cantools.database.can.message import Message
from cantools.database.can.signal import Signal
from cantools.database.can.formats.dbc import DbcSpecifics
from typing import Dict, List, Any, Annotated
from pydantic import Field
import requests
import ast
import re
from fastmcp import FastMCP
import os
import uuid
from io import BytesIO
import json
import pandas as pd
import numpy as np
import random
random.seed(42) # 固定种子值
mcp = FastMCP("dbc_tools")
DBC_STORAGE_DIR = "/tmp/dbc_store"
os.makedirs(DBC_STORAGE_DIR, exist_ok=True)


def _to_int_if_hex(value: Any) -> Any:
    """将十六进制/十进制字符串转换为整数，其他类型原样返回。"""
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"0x[0-9a-fA-F]+", s):
            return int(s, 16)
        if re.fullmatch(r"\d+", s):
            return int(s)
    return value


def _as_dict(obj: Any) -> Dict[str, Any]:
    """兼容对象/字典两种参数输入。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    # 兼容 pydantic/dataclass/普通对象
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    result = {}
    for key in dir(obj):
        if key.startswith("_"):
            continue
        try:
            value = getattr(obj, key)
            if not callable(value):
                result[key] = value
        except Exception as e:
            print(e)
    return result


def _resolve_attribute_definition(db, attr_name: str):
    if not (hasattr(db, "dbc") and db.dbc and hasattr(db.dbc, "attribute_definitions")):
        return None
    return db.dbc.attribute_definitions.get(attr_name)


def _enum_label_from_index(attr_def, raw_value: Any) -> Any:
    """将 ENUM 索引转换为标签（兼容 values/choices）。"""
    if attr_def is None:
        return raw_value

    enum_values = None
    if hasattr(attr_def, "values") and attr_def.values:
        enum_values = list(attr_def.values)
    elif hasattr(attr_def, "choices") and attr_def.choices:
        enum_values = list(attr_def.choices)

    if enum_values is None:
        return raw_value

    idx = _to_int_if_hex(raw_value)
    if isinstance(idx, int) and 0 <= idx < len(enum_values):
        return enum_values[idx]
    return raw_value


def _enum_index_from_label(attr_def, input_value: Any) -> Any:
    """将 ENUM 文本标签转换为索引；若已是数字则返回数字。"""
    numeric = _to_int_if_hex(input_value)
    if isinstance(numeric, int):
        return numeric

    if attr_def is None:
        return input_value

    enum_values = None
    if hasattr(attr_def, "values") and attr_def.values:
        enum_values = list(attr_def.values)
    elif hasattr(attr_def, "choices") and attr_def.choices:
        enum_values = list(attr_def.choices)

    if enum_values is None or not isinstance(input_value, str):
        return input_value

    for i, label in enumerate(enum_values):
        if str(label).lower() == input_value.lower():
            return i

    raise ValueError(f"ENUM属性值无效: {input_value}, 可选={enum_values}")


def _get_message_by_identifier(db, identifier: Any):
    if isinstance(identifier, int):
        return db.get_message_by_frame_id(identifier)
    return db.get_message_by_name(identifier)


def _get_signal_in_message(msg, signal_name: str):
    for sig in msg.signals:
        if sig.name == signal_name:
            return sig
    return None


def _normalize_nodes(value: Any) -> List[str]:
    """将 senders/receivers 输入统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # 兼容 "ECU1,ECU2"
        return [item.strip() for item in s.split(",") if item.strip()]
    return [str(value)]

def _get_all_nodes(db) -> List[str]:
    """
    从DBC中提取所有节点（BU_）
    """
    if hasattr(db, "nodes") and db.nodes:
        return [node.name for node in db.nodes]
    return []

def _resolve_nodes(db, value: Any) -> List[str]:
    """
    规则：
    - CCU -> 保持 CCU
    - ECU -> 从 DBC 的 BU_ 中随机选一个（排除 CCU）
    """
    nodes = _normalize_nodes(value)
    if not nodes:
        return []
    all_nodes = _get_all_nodes(db)
    # 过滤掉 CCU
    candidate_nodes = [n for n in all_nodes if n != "CCU"]
    resolved = []
    for n in nodes:
        if n == "CCU":
            resolved.append("CCU")
        elif n == "ECU":
            if not candidate_nodes:
                raise ValueError("DBC中没有可用的ECU节点（BU_）")
            resolved.append(random.choice(candidate_nodes))
        else:
            # 其他值原样保留
            resolved.append(n)
    return resolved

def _get_attr_map_from_dbc_object(obj) -> Dict[str, Any]:
    """兼容 cantools 不同版本：obj.dbc / obj.dbc_specifics。"""
    dbc_obj = getattr(obj, "dbc", None) or getattr(obj, "dbc_specifics", None)
    attrs = getattr(dbc_obj, "attributes", {}) if dbc_obj else {}
    return attrs or {}


def _query_attributes(db, identifier: Any) -> Dict[str, Any]:
    """
    获取报文的所有DBC属性，包括BA_DEF_定义和BA_值。
    """
    try:
        msg = _get_message_by_identifier(db, identifier)

        result = {
            "message_name": msg.name,
            "frame_id": hex(msg.frame_id),
            "attributes": {},
            "attribute_definitions": {}
        }

        if db.dbc and hasattr(db.dbc, "attribute_definitions"):
            for def_name, def_obj in db.dbc.attribute_definitions.items():
                kind = str(getattr(def_obj, "kind", ""))
                if "BO_" in kind:
                    enum_values = None
                    if hasattr(def_obj, "values") and def_obj.values:
                        enum_values = list(def_obj.values)
                    elif hasattr(def_obj, "choices") and def_obj.choices:
                        enum_values = list(def_obj.choices)

                    result["attribute_definitions"][def_name] = {
                        "type": getattr(def_obj, "type_name", None),
                        "default": getattr(def_obj, "default_value", None),
                        "enum_values": enum_values,
                        "min": getattr(def_obj, "minimum", None),
                        "max": getattr(def_obj, "maximum", None),
                    }

        msg_attrs = _get_attr_map_from_dbc_object(msg)
        for attr_name, attr_obj in msg_attrs.items():
            raw_value = attr_obj.value if hasattr(attr_obj, "value") else attr_obj
            attr_def = _resolve_attribute_definition(db, attr_name)
            result["attributes"][attr_name] = {
                "raw_value": raw_value,
                "resolved_value": _enum_label_from_index(attr_def, raw_value),
                "type": getattr(attr_def, "type_name", None)
            }

        return {
            "exists": True,
            "data": result
        }

    except KeyError as e:
        return {"exists": False, "error": str(e)}


def _query_attribute_definitions(db) -> Dict[str, Any]:
    definitions = {}
    if db.dbc and hasattr(db.dbc, "attribute_definitions"):
        for name, def_obj in db.dbc.attribute_definitions.items():
            enum_values = None
            if hasattr(def_obj, "values") and def_obj.values:
                enum_values = list(def_obj.values)
            elif hasattr(def_obj, "choices") and def_obj.choices:
                enum_values = list(def_obj.choices)
            definitions[name] = {
                "scope": str(getattr(def_obj, "kind", "")),
                "type": getattr(def_obj, "type_name", None),
                "default": getattr(def_obj, "default_value", None),
                "enum_values": enum_values,
                "min": getattr(def_obj, "minimum", None),
                "max": getattr(def_obj, "maximum", None),
            }

    return {
        "success": True,
        "count": len(definitions),
        "data": definitions
    }

# def _ensure_dbc_container(obj):
#     """
#     确保 message / signal 拥有 dbc.attributes 容器
#     兼容 cantools 各版本
#     """
#     dbc_obj = getattr(obj, "dbc", None)
#
#     # 没有 dbc → 创建
#     if dbc_obj is None:
#         dbc_obj = type("DBCContainer", (), {})()
#         setattr(obj, "dbc", dbc_obj)
#
#     # 没有 attributes → 创建
#     if not hasattr(dbc_obj, "attributes") or dbc_obj.attributes is None:
#         dbc_obj.attributes = {}
#
#     return dbc_obj

def _ensure_dbc_container(obj):
    """
    使用 cantools 官方 DbcSpecifics 作为容器
    """
    dbc_obj = getattr(obj, "dbc", None) or getattr(obj, "dbc_specifics", None)

    # 没有 → 创建官方容器
    if dbc_obj is None:
        from cantools.database.can.formats.dbc import DbcSpecifics
        dbc_obj = DbcSpecifics()

        # ⭐ 同时兼容两个字段（关键！）
        setattr(obj, "dbc", dbc_obj)
        setattr(obj, "dbc_specifics", dbc_obj)

    # 确保 attributes 存在
    if not hasattr(dbc_obj, "attributes") or dbc_obj.attributes is None:
        dbc_obj.attributes = {}

    return dbc_obj

def _set_message_attribute(db, identifier: Any, attr_name: str, attr_value: Any) -> Dict[str, Any]:
    msg = _get_message_by_identifier(db, identifier)

    msg_dbc = _ensure_dbc_container(msg)

    attr_def = _resolve_attribute_definition(db, attr_name)
    if attr_def is None:
        raise ValueError(f"属性定义不存在: {attr_name}")

    # 校验一下是不是该类属性
    kind = str(getattr(attr_def, "kind", ""))
    if kind != "BO_":
        raise ValueError(f"属性类型错误: {attr_name}")

    actual_value = _enum_index_from_label(attr_def, attr_value)

    if attr_name not in msg_dbc.attributes:
        from cantools.database.can.attribute import Attribute
        msg_dbc.attributes[attr_name] = Attribute(value=actual_value, definition=attr_def)
    else:
        msg_dbc.attributes[attr_name].value = actual_value

    return {
        "success": True,
        "message": f"已设置报文属性: {msg.name}.{attr_name}={attr_value}",
        "data": {
            "message": msg.name,
            "frame_id": hex(msg.frame_id),
            "attribute": attr_name,
            "raw_value": actual_value,
            "resolved_value": _enum_label_from_index(attr_def, actual_value),
        }
    }


def _set_signal_attribute(db, message_identifier: Any, signal_name: str,
                          attr_name: str, attr_value: Any) -> Dict[str, Any]:
    msg = _get_message_by_identifier(db, message_identifier)
    signal = _get_signal_in_message(msg, signal_name)
    if signal is None:
        raise ValueError(f"信号不存在: {msg.name}.{signal_name}")

    sig_dbc = _ensure_dbc_container(signal)
    attr_def = _resolve_attribute_definition(db, attr_name)
    if attr_def is None:
        raise ValueError(f"属性定义不存在: {attr_name}")
    # 校验一下是不是该类属性
    kind = str(getattr(attr_def, "kind", ""))
    if kind != "SG_":
        raise ValueError(f"属性类型错误: {attr_name}")
    actual_value = _enum_index_from_label(attr_def, attr_value)
    # todo:这里还应该判断一下kind类型
    if attr_name not in sig_dbc.attributes:
        from cantools.database.can.attribute import Attribute
        sig_dbc.attributes[attr_name] = Attribute(value=actual_value, definition=attr_def)
    else:
        sig_dbc.attributes[attr_name].value = actual_value

    return {
        "success": True,
        "message": f"已设置信号属性: {msg.name}.{signal.name}.{attr_name}={attr_value}",
        "data": {
            "message": msg.name,
            "signal": signal.name,
            "attribute": attr_name,
            "raw_value": actual_value,
            "resolved_value": _enum_label_from_index(attr_def, actual_value),
        }
    }


def _query_message_attribute(db, identifier: Any, attr_name: str) -> Dict[str, Any]:
    msg = _get_message_by_identifier(db, identifier)
    msg_attrs = _get_attr_map_from_dbc_object(msg)
    attr_def = _resolve_attribute_definition(db, attr_name)

    if attr_name in msg_attrs:
        raw_value = msg_attrs[attr_name].value if hasattr(msg_attrs[attr_name], "value") else msg_attrs[attr_name]
    else:
        raw_value = getattr(attr_def, "default_value", None)

    return {
        "success": True,
        "data": {
            "message": msg.name,
            "frame_id": hex(msg.frame_id),
            "attribute": attr_name,
            "raw_value": raw_value,
            "resolved_value": _enum_label_from_index(attr_def, raw_value),
            "type": getattr(attr_def, "type_name", None)
        }
    }


def _query_signal_attribute(db, message_identifier: Any, signal_name: str,
                            attr_name: str) -> Dict[str, Any]:
    msg = _get_message_by_identifier(db, message_identifier)
    signal = _get_signal_in_message(msg, signal_name)
    if signal is None:
        raise ValueError(f"信号不存在: {msg.name}.{signal_name}")

    sig_attrs = _get_attr_map_from_dbc_object(signal)
    attr_def = _resolve_attribute_definition(db, attr_name)

    if attr_name in sig_attrs:
        raw_value = sig_attrs[attr_name].value if hasattr(sig_attrs[attr_name], "value") else sig_attrs[attr_name]
    else:
        raw_value = getattr(attr_def, "default_value", None)

    return {
        "success": True,
        "data": {
            "message": msg.name,
            "signal": signal.name,
            "attribute": attr_name,
            "raw_value": raw_value,
            "resolved_value": _enum_label_from_index(attr_def, raw_value),
            "type": getattr(attr_def, "type_name", None)
        }
    }


def _query_attrubutes(db, identifier: Any) -> Dict[str, Any]:
    return _query_attributes(db, identifier)

def _query_message(db, identifier: Any) -> Dict[str, Any]:
    """查询报文"""
    try:
        if isinstance(identifier, int):
            msg = db.get_message_by_frame_id(identifier)
        else:
            msg = db.get_message_by_name(identifier)

        return {
            "exists": True,
            "data": {
                "name": msg.name,
                "frame_id": hex(msg.frame_id),
                "length": msg.length,
                "cycle_time": msg.cycle_time,
                "senders": msg.senders,
                "signal_count": len(msg.signals)
            }
        }
    except KeyError:
        return {"exists": False, "data": None}


def _query_signal(db, message_identifier: Any, signal_name: str) -> Dict[str, Any]:
    """查询信号"""
    try:
        if isinstance(message_identifier, int):
            msg = db.get_message_by_frame_id(message_identifier)
        else:
            msg = db.get_message_by_name(message_identifier)
    except KeyError:
        return {"exists": False, "data": None}

    signal = None
    for sig in msg.signals:
        if sig.name == signal_name:
            signal = sig
            break

    if signal:
        return {
            "exists": True,
            "data": {
                "name": signal.name,
                "start": signal.start,
                "length": signal.length,
                "scale": signal.scale,
                "offset": signal.offset,
                "unit": signal.unit,
                "byte_order": signal.byte_order,
                "is_signed": signal.is_signed
            }
        }
    else:
        return {"exists": False, "data": None}


def _add_message(db, frame_id: int, name: str, dlc: int,
                senders: List[str] = None, cycle_time: int = None,
                comment: str = None, attributes: Dict[str, Any] = None) -> Dict[str, Any]:
    """新增报文，可选同步写入报文属性。"""
    try:
        # 保留原行为：不强制检查ID冲突，由上层规划先query
        msg = Message(
            frame_id=frame_id,
            name=name,
            length=dlc,
            signals=[],
            senders=_resolve_nodes(db, senders),
            is_extended_frame=True,
            cycle_time=cycle_time,
            comment=comment
        )

        db.messages.append(msg)
        db.refresh()
        # 同步写入属性（例如 GenMsgSendType）
        if attributes:
            for attr_name, attr_value in attributes.items():
                _set_message_attribute(db, name, attr_name, attr_value)

        return {
            "success": True,
            "message": f"成功添加报文: {name} (ID: {hex(frame_id)})"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def _add_signal(db, message_identifier: Any, signal_name: str,
                start_bit: int, length: int,
                factor: float = 1.0, offset: float = 0.0,
                minimum: float = 0, maximum: float = 0,
                unit: str = "", byte_order: str = "little_endian",
                is_signed: bool = False, receivers: List[str] = None,
                comment: str = None, choices: Dict[int, str] = None,
                attributes: Dict[str, Any] = None) -> Dict[str, Any]:
    """新增信号，可选同步写入信号属性。"""
    try:
        msg = _get_message_by_identifier(db, message_identifier)

        for sig in msg.signals:
            if sig.name == signal_name:
                raise ValueError(f"信号 {signal_name} 已存在于报文 {msg.name}")

        if start_bit + length > msg.length * 8:
            raise ValueError(
                f"信号位域超出报文长度: {start_bit}+{length} > {msg.length*8}"
            )

        signal = Signal(
            name=signal_name,
            start=start_bit,
            length=length,
            byte_order=byte_order,
            is_signed=is_signed,
            scale=factor,
            offset=offset,
            comment=comment,
            minimum=minimum,
            maximum=maximum,
            unit=unit,
            choices=choices,
            receivers=_resolve_nodes(db, receivers)
        )

        msg.signals.append(signal)

        # 同步写入信号属性
        if attributes:
            for attr_name, attr_value in attributes.items():
                _set_signal_attribute(db, msg.name, signal_name, attr_name, attr_value)

        return {
            "success": True,
            "message": f"成功添加信号: {msg.name}.{signal_name}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def _delete_message(db, identifier: Any) -> Dict[str, Any]:
    """删除报文"""
    try:
        if isinstance(identifier, int):
            msg = db.get_message_by_frame_id(identifier)
        else:
            msg = db.get_message_by_name(identifier)

        msg_name = msg.name
        db.messages.remove(msg)

        return {
            "success": True,
            "message": f"成功删除报文: {msg_name}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def _delete_signal(db, message_identifier: Any, signal_name: str) -> Dict[str, Any]:
    """删除信号"""
    try:
        if isinstance(message_identifier, int):
            msg = db.get_message_by_frame_id(message_identifier)
        else:
            msg = db.get_message_by_name(message_identifier)

        signal = None
        for sig in msg.signals:
            if sig.name == signal_name:
                signal = sig
                break

        if not signal:
            raise ValueError(f"信号 {signal_name} 不存在于报文 {msg.name}")

        msg.signals.remove(signal)

        return {
            "success": True,
            "message": f"成功删除信号: {msg.name}.{signal_name}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }

# todo: 部分字段为只读字段，无法修改，例如cycle_time
def _modify_message(db, identifier: Any, modifications: Dict[str, Any]) -> Dict[str, Any]:
    """修改报文"""
    try:
        if isinstance(identifier, int):
            msg = db.get_message_by_frame_id(identifier)
        else:
            msg = db.get_message_by_name(identifier)

        for key, value in modifications.items():
            if hasattr(msg, key):
                setattr(msg, key, value)

        return {
            "success": True,
            "message": f"成功修改报文: {msg.name}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def _modify_signal(db, message_identifier: Any, signal_name: str,
                  modifications: Dict[str, Any]) -> Dict[str, Any]:
    """修改信号"""
    try:
        if isinstance(message_identifier, int):
            msg = db.get_message_by_frame_id(message_identifier)
        else:
            msg = db.get_message_by_name(message_identifier)

        signal = None
        for sig in msg.signals:
            if sig.name == signal_name:
                signal = sig
                break

        if not signal:
            raise ValueError(f"信号 {signal_name} 不存在于报文 {msg.name}")

        attr_map = {
            'factor': 'scale',
            'start_bit': 'start'
        }

        for key, value in modifications.items():
            actual_key = attr_map.get(key, key)
            if hasattr(signal, actual_key):
                setattr(signal, actual_key, value)

        return {
            "success": True,
            "message": f"成功修改信号: {msg.name}.{signal_name}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def _validate(db) -> Dict[str, Any]:
    """校验DBC文件"""
    errors = []
    warnings = []

    # 检查ID冲突
    ids = [msg.frame_id for msg in db.messages]
    duplicates = [frame_id for frame_id in ids if ids.count(frame_id) > 1]
    for dup in set(duplicates):
        errors.append(f"报文ID冲突: {hex(dup)}")

    # 检查信号位域溢出
    for msg in db.messages:
        for signal in msg.signals:
            if signal.start + signal.length > msg.length * 8:
                errors.append(f"信号位域溢出: {msg.name}.{signal.name}")

    # 检查信号名重复
    for msg in db.messages:
        signal_names = [s.name for s in msg.signals]
        dup_signals = [name for name in signal_names if signal_names.count(name) > 1]
        for dup in set(dup_signals):
            errors.append(f"信号名重复: {msg.name}.{dup}")

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "message_count": len(db.messages),
        "signal_count": sum(len(msg.signals) for msg in db.messages)
    }



# def load_dbc_from_url(url):
#     res = requests.get(url)
#     res.raise_for_status()
#     return cantools.database.load(StringIO(res.text))

def load_dbc_from_url(url):
    res = requests.get(url)
    res.raise_for_status()
    # ✅ 用 content + decode
    dbc_content = res.content.decode("utf-8")
    return cantools.database.load_string(dbc_content, 'dbc')

def load_dbc_from_string(dbc_content):
    return cantools.database.load_string(dbc_content, 'dbc')

def normalize_frame_id(frame_id):
    """统一帧ID格式，去除IDE位，保留29位有效ID"""
    if frame_id is None:
        return None
    return frame_id & 0x1FFFFFFF

def save_dbc_to_file(dbc_text: str) -> str:
    dbc_id = str(uuid.uuid4())
    path = os.path.join(DBC_STORAGE_DIR, f"{dbc_id}.dbc")

    with open(path, "w", encoding="utf-8") as f:
        f.write(dbc_text)

    return path  # 直接当 dbc_id 用

def load_dbc_from_file(path: str):
    return cantools.database.load_file(path)

# =========================
# ====== 工具1：DBC =======
# =========================
def handler(args):
    """
    DBC文件统一操作接口

    支持的操作类型:
    - query_message: 查询报文
    - query_signal: 查询信号
    - add_message: 新增报文（可带 attributes）
    - add_signal: 新增信号（可带 attributes）
    - delete_message: 删除报文
    - delete_signal: 删除信号
    - modify_message: 修改报文
    - modify_signal: 修改信号
    - validate: 校验DBC文件

    Parameters:
    args.input.operation_type - 操作类型
    args.input.dbc_file_path - DBC文本或URL
    args.input.output_path - 输出文件路径（可选）
    args.input.identifier - 报文ID或名称
    args.input.parameters - 操作参数字典
    """
    operation_type = args.input.operation_type
    dbc_file_path = args.input.dbc_file_path
    output_path = args.input.output_path
    identifier_raw = args.input.identifier
    signal_name = getattr(args.input, "signal_name", None)

    if identifier_raw is None or str(identifier_raw).strip() == "":
        identifier = None
    else:
        identifier = _to_int_if_hex(identifier_raw)

    parameters = args.input.parameters or {}
    params_dict = _as_dict(parameters)
    signals_obj = params_dict.get("signals")
    signals_dict = _as_dict(signals_obj)

    choices_str = signals_dict.get("choices")
    choices_dict = None
    if choices_str and isinstance(choices_str, str):
        choices_dict = ast.literal_eval(choices_str)
    elif isinstance(choices_str, dict):
        choices_dict = choices_str

    msg_attributes = params_dict.get("attributes") or {}
    sig_attributes = signals_dict.get("attributes") or {}
    msg_attributes = _as_dict(msg_attributes)
    sig_attributes = _as_dict(sig_attributes)
    args.logger.info(f"DBC操作: {operation_type}, 文件: {dbc_file_path}")


    try:
        # 加载DBC文件
        # db = cantools.database.load_file(dbc_file_path)
        if isinstance(dbc_file_path, str) and os.path.exists(dbc_file_path):
            # ✅ 本地文件（dbc_id）
            db = load_dbc_from_file(dbc_file_path)
        elif isinstance(dbc_file_path, str) and dbc_file_path.startswith(('http://', 'https://', 'ftp://')):
            # URL
            db = load_dbc_from_url(dbc_file_path)
        else:
            # 纯DBC字符串
            db = load_dbc_from_string(dbc_file_path)

        # 根据操作类型执行相应操作
        result = None
        if operation_type == "query_message":
            result = _query_message(db, identifier)

        elif operation_type == "query_signal":
            if not signal_name:
                raise ValueError("query_signal 需要 signal_name")
            result = _query_signal(db, identifier, signal_name)

        elif operation_type in ["query_attributes", "query_attrubutes"]:
            result = _query_attributes(db, identifier)

        elif operation_type == "query_attribute_definitions":# 提供，但不给agent调用
            result = _query_attribute_definitions(db)

        elif operation_type == "query_message_attribute":# 提供，但不给agent调用
            attr_name = params_dict.get("attribute_name") or params_dict.get("attr_name")
            if not attr_name:
                raise ValueError("query_message_attribute 需要 attribute_name")
            result = _query_message_attribute(db, identifier, attr_name)

        elif operation_type == "query_signal_attribute":# 提供，但不给agent调用
            attr_name = params_dict.get("attribute_name") or params_dict.get("attr_name")
            sig_name = signal_name or params_dict.get("signal_name") or signals_dict.get("name")
            if not sig_name or not attr_name:
                raise ValueError("query_signal_attribute 需要 signal_name 和 attribute_name")
            result = _query_signal_attribute(db, identifier, sig_name, attr_name)

        elif operation_type == "set_message_attribute":# 提供，但不给agent调用
            attr_name = params_dict.get("attribute_name") or params_dict.get("attr_name")
            attr_value = params_dict.get("attribute_value") if "attribute_value" in params_dict else params_dict.get("attr_value")
            if not attr_name:
                raise ValueError("set_message_attribute 需要 attribute_name")
            result = _set_message_attribute(db, identifier, attr_name, attr_value)

        elif operation_type == "set_signal_attribute":# 提供，但不给agent调用
            attr_name = params_dict.get("attribute_name") or params_dict.get("attr_name")
            attr_value = params_dict.get("attribute_value") if "attribute_value" in params_dict else params_dict.get("attr_value")
            sig_name = signal_name or params_dict.get("signal_name") or signals_dict.get("name")
            if not sig_name or not attr_name:
                raise ValueError("set_signal_attribute 需要 signal_name 和 attribute_name")
            result = _set_signal_attribute(db, identifier, sig_name, attr_name, attr_value)

        elif operation_type == "add_message":
            result = _add_message(
                db,
                frame_id=identifier,
                name=params_dict.get("name"),
                dlc=params_dict.get("dlc"),
                senders=params_dict.get("senders"),
                cycle_time=params_dict.get("cycle_time"),
                comment=params_dict.get("comment"),
                attributes=msg_attributes,
            )

        elif operation_type == "add_signal":
            sig_name = signals_dict.get("name")
            if not sig_name:
                raise ValueError("add_signal 需要 parameters.signals.name")
            result = _add_signal(
                db,
                message_identifier=identifier,
                signal_name=sig_name,
                start_bit=signals_dict.get("start_bit"),
                length=signals_dict.get("length"),
                factor=signals_dict.get("factor"),
                offset=signals_dict.get("offset"),
                unit=signals_dict.get("unit"),
                minimum=signals_dict.get("minimum"),
                maximum=signals_dict.get("maximum"),
                comment=signals_dict.get("comment"),
                choices=choices_dict,
                receivers=signals_dict.get("receivers"),
                attributes=sig_attributes,
            )

        elif operation_type == "delete_message":
            result = _delete_message(db, identifier)

        elif operation_type == "delete_signal":
            sig_name = signal_name or params_dict.get("signal_name")
            if not sig_name:
                raise ValueError("delete_signal 需要 signal_name")
            result = _delete_signal(db, identifier, sig_name)

        elif operation_type == "modify_message":
            result = _modify_message(db, identifier, params_dict)

        elif operation_type == "modify_signal":
            sig_name = signal_name or params_dict.get("signal_name")
            if not sig_name:
                raise ValueError("modify_signal 需要 signal_name")
            result = _modify_signal(db, identifier, sig_name, params_dict)

        elif operation_type == "validate":
            result = _validate(db)

        else:
            raise ValueError(f"不支持的操作类型: {operation_type}")

        # # 如果是修改类操作，保存DBC文件
        # if operation_type in ["add_message", "add_signal", "delete_message",
        #                      "delete_signal", "modify_message", "modify_signal"]:
        #     if not output_path:
        #         raise ValueError("修改类操作需要指定output_path")

        #     with open(output_path, 'w', encoding='utf-8') as f:
        #         f.write(db.as_dbc_string())

        args.logger.info(f"操作完成: {operation_type}")
        new_dbc_text = db.as_dbc_string()
        args.logger.info(new_dbc_text)
        # 保存到文件
        new_dbc_id = save_dbc_to_file(new_dbc_text)

        args.logger.info(f"新DBC已保存: {new_dbc_id}")

        # 不要再返回 dbc_text
        return {
            **(result if isinstance(result, dict) else {"result": result}),
            "dbc_id": new_dbc_id
        }

    except Exception as e:
        args.logger.error(f"操作失败: {str(e)}")
        raise Exception(f"DBC操作失败: {str(e)}")


@mcp.tool()
def dbc_operation(
    operation_type: Annotated[str, Field(description="操作类型：query_message / add_message / add_signal / validate 等")],
    dbc_file_path: Annotated[str, Field(description="DBC文本 / URL / dbc_id")],
    identifier: Annotated[Any, Field(description="报文ID（支持0x）或报文名称")] = None,
    signal_name: Annotated[str, Field(description="信号名称")] = None,
    parameters: Annotated[Dict[str, Any], Field(description="操作参数（signals / attributes等），具体根据operation_type参考插件接口规范")] = None,
    output_path: Annotated[str, Field(description="输出文件路径（可选）")] = None,
):
    """
    DBC文件统一操作接口（FastMCP封装版）

    等价于原 handler(args) 功能

    Parameters:
    - operation_type: 操作类型
    - dbc_file_path: DBC文本或URL
    - output_path: 输出路径（可选）
    - identifier: 报文ID或名称
    - signal_name: 信号名（部分操作需要）
    - parameters: 参数字典
    """

    # ==== 模拟原 Args 结构 ====
    class DummyLogger:
        def info(self, msg): print(msg)
        def error(self, msg): print(msg)

    class DummyInput:
        def __init__(self):
            self.operation_type = operation_type
            self.dbc_file_path = dbc_file_path
            self.output_path = output_path
            self.identifier = identifier
            self.signal_name = signal_name
            self.parameters = parameters or {}

    class DummyArgs:
        def __init__(self):
            self.input = DummyInput()
            self.logger = DummyLogger()

    args = DummyArgs()

    # ==== 直接复用原 handler ====
    return handler(args)


# =========================
# ===== 工具2：Excel解析 ====
# =========================



if not hasattr(np, "float"):
    np.float = float


def _sanitize(value):
    if pd.isna(value):
        return None
    return str(value).strip()


def _clean_headers(df_raw):
    header1 = df_raw.iloc[0]
    header2 = df_raw.iloc[1]

    headers = []
    for h1, h2 in zip(header1, header2):
        h1 = _sanitize(h1)
        h2 = _sanitize(h2)

        if h1 and h2:
            headers.append(f"{h1}_{h2}")
        elif h1:
            headers.append(h1)
        elif h2:
            headers.append(h2)
        else:
            headers.append("")
    return headers


def _detect_column(columns, keywords):
    for col in columns:
        for k in keywords:
            if k.lower() in str(col).lower():
                return col
    return None


def _parse_sheet(df_raw, logger):
    df_raw = df_raw.applymap(_sanitize)
    headers = _clean_headers(df_raw)

    df = df_raw.iloc[2:].reset_index(drop=True)
    df.columns = headers

    logger.info(f"识别列: {list(df.columns)}")


    requirement_type_col = _detect_column(df.columns, ["需求类型"])
    msg_name_col = _detect_column(df.columns, ["msg name", "message name", "报文名称"])
    msg_type_col = _detect_column(df.columns, ["msg type", "message type", "报文类型"])
    msg_id_col = _detect_column(df.columns, ["msg id", "id"])
    msg_send_type_col = _detect_column(df.columns, ["msg send type", "报文发送类型"])
    msg_description_col = _detect_column(df.columns, ["signal description", "信号描述"])
    msg_len_col = _detect_column(df.columns, ["msg length", "报文长度"])
    recv_col = _detect_column(df.columns, ["接收网段"])
    send_col = _detect_column(df.columns, ["发送网段"])
    cycle_col = _detect_column(df.columns, ["cycle", "报文周期"])

    sig_name_col = _detect_column(df.columns, ["signal name", "信号名称"])
    sig_description_col = _detect_column(df.columns, ["signal description", "信号描述"])
    start_bit_col = _detect_column(df.columns, ["start", "起始位"])
    sig_len_col = _detect_column(df.columns, ["signal length", "信号长度"])
    sig_data_type_col = _detect_column(df.columns, ["data type", "数据类型"])
    factor_col = _detect_column(df.columns, ["resolution"])
    offset_col = _detect_column(df.columns, ["offset"])
    initial_value_col = _detect_column(df.columns, ["initial value", "初始值"])
    unit_col = _detect_column(df.columns, ["unit"])
    sig_value_description_col = _detect_column(df.columns, ["signal value description", "信号值描述"])

    fill_cols = [c for c in [msg_name_col, msg_id_col, msg_len_col, cycle_col, recv_col, send_col] if c]
    if fill_cols:
        df[fill_cols] = df[fill_cols].fillna(method="ffill")

    df = df.dropna(subset=[msg_name_col])

    messages = []

    grouped = df.groupby(msg_name_col)

    for msg_name, group in grouped:
        first = group.iloc[0]

        msg = {
            "requirement_type": first.get(requirement_type_col),
            "msg_name": msg_name,
            "msg_type": first.get(msg_type_col),
            "msg_send_type": first.get(msg_send_type_col), # 中文要映射为英文，如周期->Period，这块让大模型自动转换
            "msg_id": first.get(msg_id_col),
            "msg_length": first.get(msg_len_col),
            "senders": first.get(send_col),
            "cycle_time": first.get(cycle_col),
            "msg_description": first.get(msg_description_col),
            "signals": [] # 添加报文时信号先为空
        }

        for _, row in group.iterrows():
            sig_name = row.get(sig_name_col)
            sig_len = row.get(sig_len_col)

            if not sig_name:
                continue

            if sig_len and str(sig_len).isdigit():
                sig_len = int(sig_len)
                sig_min_value = 0
                sig_max_value = (1 << sig_len) - 1
            else:
                sig_min_value = None
                sig_max_value = None

            signal = {
                "sig_name": sig_name,
                "sig_description": row.get(sig_description_col),
                "byte_order": "intel standard",
                "start_bit": row.get(start_bit_col),
                "sig_send_type": "",  # 这个和报文的发送类型关联，（周期->Period）-> Cycle, （事件->NoMsgSendType）-> OnWrite, 这块也让大模型自动转换
                "sig_length": sig_len,
                "sig_data_type": row.get(sig_data_type_col),
                "factor": row.get(factor_col),
                "offset": row.get(offset_col),
                "sig_min_value": sig_min_value,  # 自动填充
                "sig_max_value": sig_max_value,  # 自动填充
                "sig_initial_value": row.get(initial_value_col),
                "unit": row.get(unit_col),
                "sig_value_description": row.get(sig_value_description_col),  # 这里需要解析成字典形式，交给模型做转换
                "receiver": row.get(recv_col)
            }

            msg["signals"].append(signal)

        messages.append(msg)

    return messages


def _parse_excel(file_path, logger):
    logger.info(f"开始读取Excel: {file_path}")

    # 1️⃣ 读取文件内容（支持URL和本地路径）
    if isinstance(file_path, str) and file_path.startswith(('http://', 'https://', 'ftp://')):
        res = requests.get(file_path)
        res.raise_for_status()
        excel_bytes = res.content
    else:
        with open(file_path, 'rb') as f:
            excel_bytes = f.read()

    # 2️⃣ 获取Sheet列表
    xls = pd.ExcelFile(BytesIO(excel_bytes))
    all_messages = []

    for sheet in xls.sheet_names:
        logger.info(f"解析Sheet: {sheet}")
        # 3️⃣ 每次用新的 BytesIO（避免seek问题）
        df_raw = pd.read_excel(BytesIO(excel_bytes), sheet_name=sheet, header=None)

        if df_raw.empty:
            continue

        msgs = _parse_sheet(df_raw, logger)
        all_messages.extend(msgs)

    return {"messages": all_messages}


def parse_handler(args):
    file_path = args.input.file_path
    args.logger.info(f"解析需求文档: {file_path}")

    try:
        result = _parse_excel(file_path, args.logger)

        return {
            "json_result": json.dumps(result, ensure_ascii=False, indent=2)
        }

    except Exception as e:
        args.logger.error(str(e))
        return {
            "json_result": "",
            "error": str(e)
        }


@mcp.tool()
def parse_requirements(
    file_path: Annotated[str, Field(description="需求文档路径（Excel格式）")]
):
    """
    Excel需求解析工具
    """

    class DummyLogger:
        def info(self, msg): print(msg)
        def error(self, msg): print(msg)

    class DummyInput:
        def __init__(self):
            self.file_path = file_path

    class DummyArgs:
        def __init__(self):
            self.input = DummyInput()
            self.logger = DummyLogger()

    args = DummyArgs()

    return parse_handler(args)

# ==================== 服务器启动 ====================

if __name__ == "__main__":
    # 默认使用stdio传输，适合本地MCP客户端（如Claude Desktop）
    # 也可使用 'sse' 或 'http' 进行网络传输
    mcp.run(transport="http", host="0.0.0.0", port=8081)