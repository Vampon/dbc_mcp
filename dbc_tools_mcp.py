"""
DBC 文件操作 MCP 插件

围绕 SessionManager 提供一组细分工具：
  会话管理：dbc_init_session / dbc_session_info / dbc_list_nodes
  查询：    dbc_query_message / dbc_query_signal / dbc_query_attributes
  变更：    dbc_add_message / dbc_add_signal / dbc_modify_message / dbc_modify_signal /
            dbc_delete_message / dbc_delete_signal /
            dbc_set_message_attribute / dbc_set_signal_attribute
  校验/出图：dbc_validate / dbc_preview_changes / dbc_finalize / dbc_get_audit_log
  Excel：   parse_requirements

会话隔离 + 24h TTL 自动清理；每个会话独立 audit.log（JSONL）。
"""
import cantools.database
from cantools.database.can.message import Message
from cantools.database.can.signal import Signal
from cantools.database.can.formats.dbc import DbcSpecifics
from typing import Dict, List, Any, Annotated, Optional
from pydantic import Field
import requests
import ast
import re
import time
import shutil
import logging
import logging.handlers
from fastmcp import FastMCP
from starlette.responses import FileResponse, JSONResponse
from starlette.requests import Request
from urllib.parse import quote
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
DBC_FINAL_DIR = "./data/dbc_output"
DBC_SESSION_DIR = os.environ.get("DBC_SESSION_DIR", "./data/sessions")
DBC_LOG_DIR = os.environ.get("DBC_LOG_DIR", "./data/logs")
DBC_SESSION_TTL_SECONDS = int(os.environ.get("DBC_SESSION_TTL", 24 * 3600))
# 用户能访问到的 MCP 服务地址（不要带尾斜杠）
# 例：http://10.0.0.5:8081 / https://mcp.yourdomain.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8081")
os.makedirs(DBC_STORAGE_DIR, exist_ok=True)
os.makedirs(DBC_FINAL_DIR, exist_ok=True)
os.makedirs(DBC_SESSION_DIR, exist_ok=True)
os.makedirs(DBC_LOG_DIR, exist_ok=True)

# ============================================================
# 日志（#20：标准 logging 替代 print）
# ============================================================
logger = logging.getLogger("dbc_mcp")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    _file_h = logging.handlers.RotatingFileHandler(
        os.path.join(DBC_LOG_DIR, "dbc_mcp.log"),
        maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    _file_h.setFormatter(_fmt)
    _stream_h = logging.StreamHandler()
    _stream_h.setFormatter(_fmt)
    logger.addHandler(_file_h)
    logger.addHandler(_stream_h)
    logger.propagate = False


# ============================================================
# 终稿下载路由：GET /download/{file_name}
# ============================================================
@mcp.custom_route("/download/{file_name}", methods=["GET"])
async def download_dbc(request: Request):
    file_name = request.path_params["file_name"]

    # 防路径穿越
    if "/" in file_name or "\\" in file_name or ".." in file_name:
        return JSONResponse({"error": "invalid file name"}, status_code=400)

    file_path = os.path.join(DBC_FINAL_DIR, file_name)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "file not found"}, status_code=404)

    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=file_name,  # 浏览器走"下载"而非"预览"
    )


def _build_download_url(file_name: str) -> str:
    return f"{PUBLIC_BASE_URL.rstrip('/')}/download/{quote(file_name)}"


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
            logger.warning("_as_dict 读取属性失败: %s", e)
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
        # #5 硬性 ID / 名称冲突检查（不再依赖上层规划）
        for _m in db.messages:
            if _m.frame_id == frame_id:
                raise ValueError(
                    f"frame_id 冲突: {hex(frame_id)} 已被报文 {_m.name!r} 占用"
                )
            if _m.name == name:
                raise ValueError(
                    f"报文名冲突: {name!r} 已存在（frame_id={hex(_m.frame_id)}）"
                )

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


def _bits_of(start: int, length: int, byte_order: str) -> List[int]:
    """返回信号占用的"绝对位"集合，用于位域重叠检测。
    Intel(little_endian)：[start, start+length)
    Motorola(big_endian)：cantools sawtooth 展开
    """
    if str(byte_order).lower().startswith("little") or byte_order == "little_endian":
        return list(range(start, start + length))
    bits: List[int] = []
    pos = start
    for _ in range(length):
        bits.append(pos)
        if pos % 8 == 0:
            pos += 15
        else:
            pos -= 1
    return bits


def _validate(db) -> Dict[str, Any]:
    """校验DBC文件（#4 增强）"""
    errors = []
    warnings = []
    available_nodes = set(_get_all_nodes(db))

    # 1) frame_id 冲突 + 标准/扩展帧范围
    seen_ids: Dict[int, str] = {}
    for msg in db.messages:
        if msg.frame_id in seen_ids:
            errors.append(
                f"报文ID冲突: {hex(msg.frame_id)} 同时被 {seen_ids[msg.frame_id]!r} 和 {msg.name!r} 使用"
            )
        else:
            seen_ids[msg.frame_id] = msg.name

        is_ext = bool(getattr(msg, "is_extended_frame", False))
        if is_ext:
            if msg.frame_id < 0 or msg.frame_id > 0x1FFFFFFF:
                errors.append(f"扩展帧ID超出29位范围: {msg.name} {hex(msg.frame_id)}")
            elif msg.frame_id <= 0x7FF:
                warnings.append(
                    f"扩展帧但ID落在11位范围: {msg.name} {hex(msg.frame_id)}（建议确认 is_extended_frame）"
                )
        else:
            if msg.frame_id < 0 or msg.frame_id > 0x7FF:
                errors.append(
                    f"标准帧ID超出11位范围: {msg.name} {hex(msg.frame_id)}（应当 is_extended_frame=True）"
                )

    # 2) 报文名重复
    name_counts: Dict[str, int] = {}
    for msg in db.messages:
        name_counts[msg.name] = name_counts.get(msg.name, 0) + 1
    for name, n in name_counts.items():
        if n > 1:
            errors.append(f"报文名重复: {name}（共 {n} 处）")

    # 3) sender 节点引用
    for msg in db.messages:
        for sender in (msg.senders or []):
            if available_nodes and sender not in available_nodes:
                errors.append(f"报文 {msg.name} 的 sender {sender!r} 不在 BU_ 列表中")

    # 4) 信号检查（位域溢出 / 重叠 / 同报文名重复 / receiver 引用 / DLC 一致性）
    for msg in db.messages:
        sig_names: Dict[str, int] = {}
        bit_owner: Dict[int, str] = {}
        max_bit_used = -1
        for signal in msg.signals:
            sig_names[signal.name] = sig_names.get(signal.name, 0) + 1

            if signal.start + signal.length > msg.length * 8:
                errors.append(
                    f"信号位域溢出: {msg.name}.{signal.name} "
                    f"({signal.start}+{signal.length} > {msg.length*8})"
                )

            for bit in _bits_of(signal.start, signal.length, signal.byte_order):
                if bit > max_bit_used:
                    max_bit_used = bit
                if bit in bit_owner and bit_owner[bit] != signal.name:
                    errors.append(
                        f"信号位域重叠: {msg.name}.{signal.name} 与 "
                        f"{msg.name}.{bit_owner[bit]} 在 bit {bit}"
                    )
                    break
                bit_owner[bit] = signal.name

            for receiver in (signal.receivers or []):
                if available_nodes and receiver not in available_nodes:
                    errors.append(
                        f"信号 {msg.name}.{signal.name} 的 receiver {receiver!r} 不在 BU_ 列表中"
                    )

        for name, n in sig_names.items():
            if n > 1:
                errors.append(f"信号名重复: {msg.name}.{name}（共 {n} 处）")

        # DLC 一致性（warning）
        if msg.signals and max_bit_used >= 0:
            needed_bytes = (max_bit_used // 8) + 1
            if needed_bytes > msg.length:
                errors.append(
                    f"DLC 不足: {msg.name} 的信号需要 {needed_bytes} 字节，但 DLC={msg.length}"
                )
            elif needed_bytes < msg.length and msg.length - needed_bytes >= 2:
                warnings.append(
                    f"DLC 偏大: {msg.name} 的信号仅占用 {needed_bytes} 字节，DLC={msg.length}"
                )

    # 5) 跨报文信号名重复（warning）
    cross: Dict[str, List[str]] = {}
    for msg in db.messages:
        for sig in msg.signals:
            cross.setdefault(sig.name, []).append(msg.name)
    for sig_name, owners in cross.items():
        if len(owners) > 1:
            warnings.append(f"信号名跨报文重复: {sig_name} 出现于 {owners}")

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
    raw = res.content
    # ✅ 多编码兜底（按概率排序）
    for encoding in ["gbk", "utf-8", "cp1252", "latin-1"]:
        try:
            dbc_content = raw.decode(encoding)
            return cantools.database.load_string(dbc_content, 'dbc')
        except Exception:
            continue

    # ✅ 最后兜底（绝不报错）
    dbc_content = raw.decode("latin-1", errors="ignore")
    return cantools.database.load_string(dbc_content, 'dbc')

def load_dbc_from_string(dbc_content):
    return cantools.database.load_string(dbc_content, 'dbc')

def normalize_frame_id(frame_id):
    """统一帧ID格式，去除IDE位，保留29位有效ID"""
    if frame_id is None:
        return None
    return frame_id & 0x1FFFFFFF

def save_dbc_to_file(dbc_text: str) -> str:
    """
    保存过程中临时DBC文件
    """
    dbc_id = str(uuid.uuid4())
    path = os.path.join(DBC_STORAGE_DIR, f"{dbc_id}.dbc")

    with open(path, "w", encoding="gbk") as f:
        f.write(dbc_text)

    return path  # 直接当 dbc_id 用

def save_final_dbc(dbc_text: str, name_hint: str = None) -> str:
    """
    保存最终DBC文件
    """
    if name_hint:
        file_name = f"{name_hint}.dbc"
    else:
        file_name = f"final_{uuid.uuid4().hex[:8]}.dbc"
    path = os.path.join(DBC_FINAL_DIR, file_name)
    with open(path, "w", newline='', encoding="gbk", errors='ignore') as f:
        f.write(dbc_text)
    return path

def load_dbc_from_file(path: str):
    return cantools.database.load_file(path)


# ============================================================
# Excel 需求解析
# ============================================================



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
        def info(self, msg): logger.info(msg)
        def error(self, msg): logger.error(msg)

    class DummyInput:
        def __init__(self):
            self.file_path = file_path

    class DummyArgs:
        def __init__(self):
            self.input = DummyInput()
            self.logger = DummyLogger()

    args = DummyArgs()

    return parse_handler(args)

# ============================================================
# Session 化 + 审计 + 清理（#6 #13 #24）
# ============================================================
_SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _is_valid_session_id(s: Any) -> bool:
    return isinstance(s, str) and bool(_SESSION_ID_RE.match(s))


class SessionManager:
    """每个会话独立目录：original.dbc / current.dbc / meta.json / audit.log
    机会主义清理过期 session（#13）。
    """

    def __init__(self, base_dir: str, ttl_seconds: int):
        self.base_dir = base_dir
        self.ttl_seconds = ttl_seconds
        os.makedirs(base_dir, exist_ok=True)

    def _dir(self, sid: str) -> str:
        if not _is_valid_session_id(sid):
            raise ValueError(f"无效的 session_id: {sid!r}")
        return os.path.join(self.base_dir, sid)

    def _ensure(self, sid: str) -> str:
        d = self._dir(sid)
        if not os.path.isdir(d):
            raise ValueError(f"session 不存在或已过期: {sid}")
        return d

    @staticmethod
    def _orig_path(d): return os.path.join(d, "original.dbc")
    @staticmethod
    def _curr_path(d): return os.path.join(d, "current.dbc")
    @staticmethod
    def _meta_path(d): return os.path.join(d, "meta.json")
    @staticmethod
    def _audit_path(d): return os.path.join(d, "audit.log")

    def _read_meta(self, sid):
        d = self._ensure(sid)
        with open(self._meta_path(d), "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_meta(self, sid, meta):
        d = self._ensure(sid)
        with open(self._meta_path(d), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _touch(self, sid):
        meta = self._read_meta(sid)
        meta["last_op_at"] = time.time()
        self._write_meta(sid, meta)

    # ---- 审计日志（#24）----
    def append_audit(self, sid, op, params, result, error=None):
        try:
            d = self._ensure(sid)
        except Exception:
            return
        try:
            meta = self._read_meta(sid)
            user_id = meta.get("user_id")
        except Exception:
            user_id = None
        try:
            params_s = json.dumps(params, ensure_ascii=False, default=str)
            if len(params_s) > 800:
                params_s = params_s[:800] + "...[truncated]"
        except Exception:
            params_s = str(params)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": sid,
            "user_id": user_id,
            "op": op,
            "params": params_s,
            "result": result,
        }
        if error:
            entry["error"] = str(error)
        with open(self._audit_path(d), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_audit(self, sid):
        d = self._ensure(sid)
        path = self._audit_path(d)
        if not os.path.isfile(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    # ---- 会话生命周期 ----
    def create(self, dbc_source, user_id=None, source_label=None):
        self.cleanup_expired()
        # 复用 v1 的加载逻辑
        if isinstance(dbc_source, str) and os.path.exists(dbc_source):
            db = load_dbc_from_file(dbc_source)
        elif isinstance(dbc_source, str) and dbc_source.startswith(("http://", "https://", "ftp://")):
            db = load_dbc_from_url(dbc_source)
        else:
            db = load_dbc_from_string(dbc_source)
        text = db.as_dbc_string()

        sid = uuid.uuid4().hex
        d = os.path.join(self.base_dir, sid)
        os.makedirs(d, exist_ok=True)
        with open(self._orig_path(d), "w", encoding="gbk", newline="") as f:
            f.write(text)
        with open(self._curr_path(d), "w", encoding="gbk", newline="") as f:
            f.write(text)

        now = time.time()
        meta = {
            "session_id": sid,
            "user_id": user_id,
            "source": source_label or (dbc_source[:200] if isinstance(dbc_source, str) else None),
            "created_at": now,
            "last_op_at": now,
        }
        with open(self._meta_path(d), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self.append_audit(sid, "init_session",
                          {"source": meta["source"], "user_id": user_id}, "success")
        logger.info("session 创建: %s (user=%s)", sid, user_id)
        return {
            "session_id": sid,
            "message_count": len(db.messages),
            "signal_count": sum(len(m.signals) for m in db.messages),
            "node_count": len(_get_all_nodes(db)),
        }

    def load_current(self, sid):
        d = self._ensure(sid)
        return cantools.database.load_file(self._curr_path(d))

    def load_original(self, sid):
        d = self._ensure(sid)
        return cantools.database.load_file(self._orig_path(d))

    def save_current(self, sid, db):
        d = self._ensure(sid)
        with open(self._curr_path(d), "w", encoding="gbk", newline="") as f:
            f.write(db.as_dbc_string())
        self._touch(sid)

    def finalize(self, sid, name_hint=None):
        d = self._ensure(sid)
        if name_hint:
            safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", name_hint).strip("_") or "final"
            file_name = f"{safe}_{sid[:8]}.dbc"
        else:
            file_name = f"final_{sid[:8]}.dbc"
        target = os.path.join(DBC_FINAL_DIR, file_name)
        shutil.copyfile(self._curr_path(d), target)
        self.append_audit(sid, "finalize", {"name_hint": name_hint}, "success")
        logger.info("session 落盘最终: %s -> %s", sid, target)
        return {"final_path": target, "file_name": file_name}

    def cleanup_expired(self):
        if not os.path.isdir(self.base_dir):
            return 0
        now = time.time()
        removed = 0
        for sid in os.listdir(self.base_dir):
            sdir = os.path.join(self.base_dir, sid)
            if not os.path.isdir(sdir):
                continue
            meta_path = os.path.join(sdir, "meta.json")
            try:
                if os.path.isfile(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    last = meta.get("last_op_at", meta.get("created_at", 0))
                else:
                    last = os.path.getmtime(sdir)
            except Exception:
                last = 0
            if now - last > self.ttl_seconds:
                try:
                    shutil.rmtree(sdir)
                    removed += 1
                except Exception as e:
                    logger.warning("清理 session %s 失败: %s", sid, e)
        if removed:
            logger.info("清理过期 session %d 个", removed)
        return removed


session_mgr = SessionManager(DBC_SESSION_DIR, DBC_SESSION_TTL_SECONDS)


def _audit_safe(sid, op, params, result_obj=None, error=None):
    try:
        if error is not None:
            session_mgr.append_audit(sid, op, params, "error", error)
        elif result_obj is not None:
            ok = result_obj.get("success", True) if isinstance(result_obj, dict) else True
            session_mgr.append_audit(sid, op, params, "success" if ok else "error",
                                     result_obj.get("error") if isinstance(result_obj, dict) else None)
    except Exception as e:
        logger.warning("audit 记录失败: %s", e)


def _err(e: Exception, op: str) -> Dict[str, Any]:
    logger.error("[%s] %s", op, e)
    return {"success": False, "error": str(e), "error_type": type(e).__name__}


# ============================================================
# diff（#23）
# ============================================================
def _msg_to_dict(msg) -> Dict[str, Any]:
    return {
        "name": msg.name,
        "frame_id": msg.frame_id,
        "frame_id_hex": hex(msg.frame_id),
        "length": msg.length,
        "cycle_time": getattr(msg, "cycle_time", None),
        "senders": list(msg.senders or []),
        "comment": msg.comment,
        "is_extended_frame": getattr(msg, "is_extended_frame", None),
        "attributes": {k: (v.value if hasattr(v, "value") else v)
                       for k, v in _get_attr_map_from_dbc_object(msg).items()},
        "signals": {sig.name: _sig_to_dict(sig) for sig in msg.signals},
    }


def _sig_to_dict(sig) -> Dict[str, Any]:
    return {
        "name": sig.name,
        "start": sig.start,
        "length": sig.length,
        "byte_order": sig.byte_order,
        "is_signed": sig.is_signed,
        "scale": sig.scale,
        "offset": sig.offset,
        "minimum": sig.minimum,
        "maximum": sig.maximum,
        "unit": sig.unit,
        "comment": sig.comment,
        "choices": {int(k): str(v) for k, v in sig.choices.items()} if sig.choices else None,
        "receivers": list(sig.receivers or []),
        "attributes": {k: (v.value if hasattr(v, "value") else v)
                       for k, v in _get_attr_map_from_dbc_object(sig).items()},
    }


_MSG_DIFF_FIELDS = ["name", "length", "cycle_time", "senders", "comment",
                    "is_extended_frame", "attributes"]
_SIG_DIFF_FIELDS = ["start", "length", "byte_order", "is_signed", "scale", "offset",
                    "minimum", "maximum", "unit", "comment", "choices",
                    "receivers", "attributes"]


def _msg_brief(m):
    return {"name": m["name"], "frame_id": m["frame_id_hex"], "length": m["length"],
            "cycle_time": m["cycle_time"], "senders": m["senders"],
            "signal_count": len(m["signals"])}


def _sig_brief(s):
    return {"name": s["name"], "start": s["start"], "length": s["length"],
            "byte_order": s["byte_order"], "unit": s["unit"]}


def _diff_dbc(orig_db, curr_db):
    o = {m.frame_id: _msg_to_dict(m) for m in orig_db.messages}
    c = {m.frame_id: _msg_to_dict(m) for m in curr_db.messages}
    added_ids = sorted(set(c) - set(o))
    removed_ids = sorted(set(o) - set(c))
    common_ids = sorted(set(o) & set(c))

    added = [_msg_brief(c[i]) for i in added_ids]
    removed = [_msg_brief(o[i]) for i in removed_ids]
    modified = []
    sa = sr = sm = 0

    for fid in common_ids:
        om, cm = o[fid], c[fid]
        msg_changes = {}
        for k in _MSG_DIFF_FIELDS:
            if om.get(k) != cm.get(k):
                msg_changes[k] = {"old": om.get(k), "new": cm.get(k)}
        os_, cs_ = om["signals"], cm["signals"]
        sig_added = [_sig_brief(cs_[s]) for s in sorted(set(cs_) - set(os_))]
        sig_removed = [_sig_brief(os_[s]) for s in sorted(set(os_) - set(cs_))]
        sig_modified = []
        for s in sorted(set(os_) & set(cs_)):
            oss, css = os_[s], cs_[s]
            sc = {}
            for k in _SIG_DIFF_FIELDS:
                if oss.get(k) != css.get(k):
                    sc[k] = {"old": oss.get(k), "new": css.get(k)}
            if sc:
                sig_modified.append({"name": s, "field_changes": sc})
        sa += len(sig_added)
        sr += len(sig_removed)
        sm += len(sig_modified)
        if msg_changes or sig_added or sig_removed or sig_modified:
            modified.append({
                "name": cm["name"],
                "frame_id": cm["frame_id_hex"],
                "field_changes": msg_changes,
                "signals": {"added": sig_added, "removed": sig_removed, "modified": sig_modified},
            })

    return {
        "summary": {
            "messages_added": len(added),
            "messages_removed": len(removed),
            "messages_modified": len(modified),
            "signals_added": sa,
            "signals_removed": sr,
            "signals_modified": sm,
        },
        "messages": {"added": added, "removed": removed, "modified": modified},
    }


# ============================================================
# 新 MCP 工具（#15 拆细）—— 全部基于 session_id
# ============================================================
@mcp.tool()
def dbc_init_session(
    dbc_source: Annotated[str, Field(description="DBC 来源：HTTP(S) URL / 本地路径 / DBC 文本")],
    user_id: Annotated[Optional[str], Field(description="用户标识（用于审计与隔离）")] = None,
    source_label: Annotated[Optional[str], Field(description="来源备注")] = None,
) -> Dict[str, Any]:
    """初始化 DBC 会话，返回 session_id。后续所有 dbc_* 工具用 session_id 引用。会话默认 24h 过期自动清理。"""
    op = "dbc_init_session"
    try:
        info = session_mgr.create(dbc_source, user_id=user_id, source_label=source_label)
        return {"success": True, "data": info}
    except Exception as e:
        logger.exception("init_session 失败")
        return _err(e, op)


@mcp.tool()
def dbc_session_info(
    session_id: Annotated[str, Field(description="会话ID")],
) -> Dict[str, Any]:
    """查询会话当前状态（统计 + 节点列表 + meta）。"""
    op = "dbc_session_info"
    try:
        meta = session_mgr._read_meta(session_id)
        db = session_mgr.load_current(session_id)
        return {
            "success": True,
            "data": {
                **meta,
                "message_count": len(db.messages),
                "signal_count": sum(len(m.signals) for m in db.messages),
                "nodes": _get_all_nodes(db),
            },
        }
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_list_nodes(
    session_id: Annotated[str, Field(description="会话ID")],
) -> Dict[str, Any]:
    """列出 DBC 中所有 BU_ 节点名。"""
    op = "dbc_list_nodes"
    try:
        db = session_mgr.load_current(session_id)
        nodes = _get_all_nodes(db)
        return {"success": True, "data": {"nodes": nodes, "count": len(nodes)}}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_query_message(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID（支持 0x）或报文名")],
) -> Dict[str, Any]:
    """查询报文。"""
    op = "dbc_query_message"
    try:
        db = session_mgr.load_current(session_id)
        return {"success": True, **_query_message(db, _to_int_if_hex(identifier))}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_query_signal(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    signal_name: Annotated[str, Field(description="信号名")],
) -> Dict[str, Any]:
    """查询信号。"""
    op = "dbc_query_signal"
    try:
        db = session_mgr.load_current(session_id)
        return {"success": True, **_query_signal(db, _to_int_if_hex(identifier), signal_name)}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_query_attributes(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
) -> Dict[str, Any]:
    """查询报文的所有 DBC 属性。"""
    op = "dbc_query_attributes"
    try:
        db = session_mgr.load_current(session_id)
        return {"success": True, **_query_attributes(db, _to_int_if_hex(identifier))}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_add_message(
    session_id: Annotated[str, Field(description="会话ID")],
    frame_id: Annotated[str, Field(description="报文ID（支持 0x 前缀十六进制或十进制）")],
    name: Annotated[str, Field(description="报文名称")],
    dlc: Annotated[int, Field(description="数据长度（字节）", ge=1, le=64)],
    senders: Annotated[Optional[List[str]], Field(description="发送节点列表")] = None,
    cycle_time: Annotated[Optional[int], Field(description="周期时间（ms）")] = None,
    comment: Annotated[Optional[str], Field(description="报文注释")] = None,
    attributes: Annotated[Optional[Dict[str, Any]], Field(description="DBC 属性，例如 {GenMsgSendType: Period}")] = None,
) -> Dict[str, Any]:
    """新增报文。frame_id / 报文名冲突时直接报错（#5）。"""
    op = "dbc_add_message"
    params = {"frame_id": frame_id, "name": name, "dlc": dlc, "senders": senders,
              "cycle_time": cycle_time, "attributes": attributes}
    try:
        db = session_mgr.load_current(session_id)
        fid = _to_int_if_hex(frame_id)
        if not isinstance(fid, int):
            raise ValueError(f"frame_id 必须是整数: {frame_id!r}")
        result = _add_message(db, frame_id=fid, name=name, dlc=int(dlc),
                              senders=senders, cycle_time=cycle_time,
                              comment=comment, attributes=attributes)
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_add_signal(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="目标报文ID 或报文名")],
    signal_name: Annotated[str, Field(description="信号名")],
    start_bit: Annotated[int, Field(description="起始位")],
    length: Annotated[int, Field(description="信号长度（bit）", ge=1)],
    factor: Annotated[float, Field(description="比例因子")] = 1.0,
    offset: Annotated[float, Field(description="偏移量")] = 0.0,
    minimum: Annotated[float, Field(description="物理最小值")] = 0.0,
    maximum: Annotated[float, Field(description="物理最大值")] = 0.0,
    unit: Annotated[str, Field(description="单位")] = "",
    byte_order: Annotated[str, Field(description="字节序，例如 little_endian / big_endian")] = "little_endian",
    is_signed: Annotated[bool, Field(description="是否有符号")] = False,
    receivers: Annotated[Optional[List[str]], Field(description="接收节点列表")] = None,
    comment: Annotated[Optional[str], Field(description="信号注释")] = None,
    choices: Annotated[Optional[Dict[str, str]], Field(description="枚举映射，键为整数字符串")] = None,
    attributes: Annotated[Optional[Dict[str, Any]], Field(description="DBC 信号属性")] = None,
) -> Dict[str, Any]:
    """新增信号。"""
    op = "dbc_add_signal"
    params = {"identifier": identifier, "signal_name": signal_name,
              "start_bit": start_bit, "length": length}
    try:
        db = session_mgr.load_current(session_id)
        choices_int = None
        if choices:
            choices_int = {}
            for k, v in choices.items():
                ki = _to_int_if_hex(k)
                if not isinstance(ki, int):
                    raise ValueError(f"choices 的键必须是整数: {k!r}")
                choices_int[ki] = str(v)
        result = _add_signal(db, message_identifier=_to_int_if_hex(identifier),
                             signal_name=signal_name,
                             start_bit=int(start_bit), length=int(length),
                             factor=float(factor), offset=float(offset),
                             minimum=float(minimum), maximum=float(maximum),
                             unit=unit or "", byte_order=byte_order,
                             is_signed=bool(is_signed),
                             receivers=receivers, comment=comment,
                             choices=choices_int, attributes=attributes)
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_modify_message(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    modifications: Annotated[Dict[str, Any], Field(description="字段 → 新值")],
) -> Dict[str, Any]:
    """修改报文。"""
    op = "dbc_modify_message"
    params = {"identifier": identifier, "modifications": modifications}
    try:
        db = session_mgr.load_current(session_id)
        result = _modify_message(db, _to_int_if_hex(identifier), modifications or {})
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_modify_signal(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    signal_name: Annotated[str, Field(description="信号名")],
    modifications: Annotated[Dict[str, Any], Field(description="字段 → 新值")],
) -> Dict[str, Any]:
    """修改信号。"""
    op = "dbc_modify_signal"
    params = {"identifier": identifier, "signal_name": signal_name,
              "modifications": modifications}
    try:
        db = session_mgr.load_current(session_id)
        result = _modify_signal(db, _to_int_if_hex(identifier), signal_name, modifications or {})
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_delete_message(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
) -> Dict[str, Any]:
    """删除报文。"""
    op = "dbc_delete_message"
    params = {"identifier": identifier}
    try:
        db = session_mgr.load_current(session_id)
        result = _delete_message(db, _to_int_if_hex(identifier))
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_delete_signal(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    signal_name: Annotated[str, Field(description="信号名")],
) -> Dict[str, Any]:
    """删除信号。"""
    op = "dbc_delete_signal"
    params = {"identifier": identifier, "signal_name": signal_name}
    try:
        db = session_mgr.load_current(session_id)
        result = _delete_signal(db, _to_int_if_hex(identifier), signal_name)
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_set_message_attribute(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    attribute_name: Annotated[str, Field(description="属性名")],
    attribute_value: Annotated[Any, Field(description="属性值")],
) -> Dict[str, Any]:
    """设置报文级属性（BO_）。"""
    op = "dbc_set_message_attribute"
    params = {"identifier": identifier, "attribute_name": attribute_name,
              "attribute_value": attribute_value}
    try:
        db = session_mgr.load_current(session_id)
        result = _set_message_attribute(db, _to_int_if_hex(identifier),
                                        attribute_name, attribute_value)
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_set_signal_attribute(
    session_id: Annotated[str, Field(description="会话ID")],
    identifier: Annotated[str, Field(description="报文ID 或报文名")],
    signal_name: Annotated[str, Field(description="信号名")],
    attribute_name: Annotated[str, Field(description="属性名")],
    attribute_value: Annotated[Any, Field(description="属性值")],
) -> Dict[str, Any]:
    """设置信号级属性（SG_）。"""
    op = "dbc_set_signal_attribute"
    params = {"identifier": identifier, "signal_name": signal_name,
              "attribute_name": attribute_name, "attribute_value": attribute_value}
    try:
        db = session_mgr.load_current(session_id)
        result = _set_signal_attribute(db, _to_int_if_hex(identifier), signal_name,
                                       attribute_name, attribute_value)
        if result.get("success"):
            session_mgr.save_current(session_id, db)
        _audit_safe(session_id, op, params, result)
        return result
    except Exception as e:
        _audit_safe(session_id, op, params, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_validate(
    session_id: Annotated[str, Field(description="会话ID")],
) -> Dict[str, Any]:
    """校验当前 DBC（#4 增强）。"""
    op = "dbc_validate"
    try:
        db = session_mgr.load_current(session_id)
        result = _validate(db)
        _audit_safe(session_id, op, {}, {"success": result["is_valid"]})
        return {"success": True, "data": result}
    except Exception as e:
        _audit_safe(session_id, op, {}, error=e)
        return _err(e, op)


@mcp.tool()
def dbc_preview_changes(
    session_id: Annotated[str, Field(description="会话ID")],
) -> Dict[str, Any]:
    """返回 original.dbc → current.dbc 的结构化 diff，用于人工确认（#23）。"""
    op = "dbc_preview_changes"
    try:
        orig = session_mgr.load_original(session_id)
        curr = session_mgr.load_current(session_id)
        diff = _diff_dbc(orig, curr)
        return {"success": True, "data": diff}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_finalize(
    session_id: Annotated[str, Field(description="会话ID")],
    name_hint: Annotated[Optional[str], Field(description="最终文件名前缀（可选）")] = None,
) -> Dict[str, Any]:
    """将当前 DBC 落盘到最终输出目录。建议在 dbc_validate + dbc_preview_changes 通过后调用。
    返回的 download_url 可直接交给用户下载。"""
    op = "dbc_finalize"
    try:
        info = session_mgr.finalize(session_id, name_hint=name_hint)
        info["download_url"] = _build_download_url(info["file_name"])
        return {"success": True, "data": info}
    except Exception as e:
        return _err(e, op)


@mcp.tool()
def dbc_get_audit_log(
    session_id: Annotated[str, Field(description="会话ID")],
) -> Dict[str, Any]:
    """读取该会话的审计日志（#24）。"""
    op = "dbc_get_audit_log"
    try:
        entries = session_mgr.read_audit(session_id)
        return {"success": True, "data": {"count": len(entries), "entries": entries}}
    except Exception as e:
        return _err(e, op)


# ==================== 服务器启动 ====================

if __name__ == "__main__":
    # 默认使用stdio传输，适合本地MCP客户端（如Claude Desktop）
    # 也可使用 'sse' 或 'http' 进行网络传输
    logger.info("启动 dbc_tools MCP，session_dir=%s, ttl=%ds",
                DBC_SESSION_DIR, DBC_SESSION_TTL_SECONDS)
    session_mgr.cleanup_expired()
    mcp.run(transport="http", host="0.0.0.0", port=8081)