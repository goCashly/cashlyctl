from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommandKind(StrEnum):
    EMPTY = "EMPTY"
    INIT_ADMIN = "INIT_ADMIN"
    LOGON = "LOGON"
    LOGOFF = "LOGOFF"
    SERVICE_ON = "SERVICE_ON"
    PROCEED = "PROCEED"
    HELP = "HELP"
    EXIT = "EXIT"
    MENU_ROOT = "MENU_ROOT"
    PANEL_JUMP = "PANEL_JUMP"
    REFRESH = "REFRESH"
    PROFILE = "PROFILE"
    SET_ENV = "SET_ENV"
    SET_STATE = "SET_STATE"
    TAIL = "TAIL"
    DETAIL = "DETAIL"
    DEPLOY = "DEPLOY"
    ROLLBACK = "ROLLBACK"
    STATUS_DEPLOY = "STATUS_DEPLOY"
    DIFF = "DIFF"
    PLAN = "PLAN"
    CONFIRM_DEPLOY = "CONFIRM_DEPLOY"
    AWS_SSO_STATUS = "AWS_SSO_STATUS"
    AWS_SSO_LOGIN = "AWS_SSO_LOGIN"
    CRM_PAIR = "CRM_PAIR"
    CRM_STATUS = "CRM_STATUS"
    CRM_NEXT = "CRM_NEXT"
    SAVE_QRY = "SAVE_QRY"
    NUMBER = "NUMBER"
    RAW = "RAW"


@dataclass(slots=True)
class ParsedCommand:
    kind: CommandKind
    raw: str
    value: str = ""


def parse_command(text: str) -> ParsedCommand:
    raw = text.strip()
    if not raw:
        return ParsedCommand(kind=CommandKind.EMPTY, raw=text)

    upper = raw.upper()
    if upper == "INITADMIN" or upper.startswith("INITADMIN "):
        return ParsedCommand(kind=CommandKind.INIT_ADMIN, raw=text, value=raw[9:].strip())
    if upper == "INIT ADMIN" or upper.startswith("INIT ADMIN "):
        return ParsedCommand(kind=CommandKind.INIT_ADMIN, raw=text, value=raw[10:].strip())
    if upper == "SETUP ADMIN" or upper.startswith("SETUP ADMIN "):
        return ParsedCommand(kind=CommandKind.INIT_ADMIN, raw=text, value=raw[11:].strip())
    if upper == "LOGON" or upper.startswith("LOGON "):
        return ParsedCommand(kind=CommandKind.LOGON, raw=text, value=raw[5:].strip())
    if upper == "L" or upper.startswith("L "):
        return ParsedCommand(kind=CommandKind.LOGON, raw=text, value=raw[1:].strip())
    if upper in {"LOGOFF", "LOGOUT"}:
        return ParsedCommand(kind=CommandKind.LOGOFF, raw=text)
    if upper == "SERVICE ON":
        return ParsedCommand(kind=CommandKind.SERVICE_ON, raw=text)
    if upper == "PROCEED":
        return ParsedCommand(kind=CommandKind.PROCEED, raw=text, value="")
    if upper.startswith("PROCEED "):
        return ParsedCommand(kind=CommandKind.PROCEED, raw=text, value=raw[8:].strip())
    if upper in {"HELP", "?"}:
        return ParsedCommand(kind=CommandKind.HELP, raw=text)
    if upper in {"EXIT", "=X"}:
        return ParsedCommand(kind=CommandKind.EXIT, raw=text)
    if upper == "=0":
        return ParsedCommand(kind=CommandKind.MENU_ROOT, raw=text)
    if raw.startswith("=") and raw[1:].isdigit():
        return ParsedCommand(kind=CommandKind.PANEL_JUMP, raw=text, value=raw[1:])
    if upper == "REFRESH":
        return ParsedCommand(kind=CommandKind.REFRESH, raw=text)
    if upper == "PROFILE":
        return ParsedCommand(kind=CommandKind.PROFILE, raw=text)
    if upper.startswith("SET ENV "):
        return ParsedCommand(kind=CommandKind.SET_ENV, raw=text, value=raw[8:].strip())
    if upper.startswith("SET STATE "):
        return ParsedCommand(kind=CommandKind.SET_STATE, raw=text, value=raw[10:].strip())
    if upper.startswith("STATE "):
        return ParsedCommand(kind=CommandKind.SET_STATE, raw=text, value=raw[6:].strip())
    if upper.startswith("TAIL "):
        return ParsedCommand(kind=CommandKind.TAIL, raw=text, value=raw[5:].strip())
    if upper.startswith("DETAIL "):
        return ParsedCommand(kind=CommandKind.DETAIL, raw=text, value=raw[7:].strip())
    if upper.startswith("OPEN "):
        return ParsedCommand(kind=CommandKind.DETAIL, raw=text, value=raw[5:].strip())
    if upper.startswith("DEPLOY "):
        return ParsedCommand(kind=CommandKind.DEPLOY, raw=text, value=raw[7:].strip())
    if upper.startswith("ROLLBACK "):
        return ParsedCommand(kind=CommandKind.ROLLBACK, raw=text, value=raw[9:].strip())
    if upper.startswith("STATUS DEPLOY "):
        return ParsedCommand(kind=CommandKind.STATUS_DEPLOY, raw=text, value=raw[14:].strip())
    if upper.startswith("DIFF "):
        return ParsedCommand(kind=CommandKind.DIFF, raw=text, value=raw[5:].strip())
    if upper == "PLAN":
        return ParsedCommand(kind=CommandKind.PLAN, raw=text, value="")
    if upper.startswith("PLAN "):
        return ParsedCommand(kind=CommandKind.PLAN, raw=text, value=raw[5:].strip())
    if upper.startswith("CONFIRM DEPLOY "):
        return ParsedCommand(kind=CommandKind.CONFIRM_DEPLOY, raw=text, value=raw[15:].strip())
    if upper in {"SSO STATUS", "AWS SSO STATUS"}:
        return ParsedCommand(kind=CommandKind.AWS_SSO_STATUS, raw=text)
    if upper in {"SSO LOGIN", "AWS SSO LOGIN"}:
        return ParsedCommand(kind=CommandKind.AWS_SSO_LOGIN, raw=text, value="")
    if upper.startswith("SSO LOGIN "):
        return ParsedCommand(kind=CommandKind.AWS_SSO_LOGIN, raw=text, value=raw[10:].strip())
    if upper.startswith("AWS SSO LOGIN "):
        return ParsedCommand(kind=CommandKind.AWS_SSO_LOGIN, raw=text, value=raw[14:].strip())
    if upper in {"CRM STATUS", "CASHLYCRM STATUS", "CRM WHOAMI", "CASHLYCRM WHOAMI"}:
        return ParsedCommand(kind=CommandKind.CRM_STATUS, raw=text)
    if upper in {
        "CRM NEXT",
        "CRM NEXT CONTACT",
        "CASHLYCRM NEXT",
        "CASHLYCRM NEXT CONTACT",
        "MACRO NEXT",
        "MACRO NEXT CONTACT",
        "NEXT CONTACT",
    }:
        return ParsedCommand(kind=CommandKind.CRM_NEXT, raw=text)
    if upper == "CRM PAIR" or upper.startswith("CRM PAIR "):
        return ParsedCommand(kind=CommandKind.CRM_PAIR, raw=text, value=raw[8:].strip())
    if upper == "CASHLYCRM PAIR" or upper.startswith("CASHLYCRM PAIR "):
        return ParsedCommand(kind=CommandKind.CRM_PAIR, raw=text, value=raw[14:].strip())
    if upper == "CRM AUTH" or upper.startswith("CRM AUTH "):
        return ParsedCommand(kind=CommandKind.CRM_PAIR, raw=text, value=raw[8:].strip())
    if upper.startswith("SAVE QRY "):
        return ParsedCommand(kind=CommandKind.SAVE_QRY, raw=text, value=raw[9:].strip())
    if raw.isdigit():
        return ParsedCommand(kind=CommandKind.NUMBER, raw=text, value=raw)
    return ParsedCommand(kind=CommandKind.RAW, raw=text, value=raw)
