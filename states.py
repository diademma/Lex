# --- START OF FILE states.py ---

from aiogram.fsm.state import State, StatesGroup


class AdminState(StatesGroup):
    waiting_for_add = State()
    waiting_for_del = State()


class TagState(StatesGroup):
    waiting_for_add = State()
    waiting_for_del = State()


class OwnerFSM(StatesGroup):
    q1 = State()
    q2 = State()
    q3 = State()
    waiting_for_owner_id = State()


class PatchState(StatesGroup):
    collecting = State()


class LexState(StatesGroup):
    listening = State()


class ReposterFSM(StatesGroup):
    waiting_source      = State()   # ждём ввода нового канала-источника
    waiting_label       = State()   # ждём метку для канала
    waiting_interval    = State()   # ждём пользовательский интервал (минуты)
    waiting_json_filter = State()   # ждём JSON-фильтр от пользователя
