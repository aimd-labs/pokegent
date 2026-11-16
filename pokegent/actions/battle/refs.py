"""Executor and postcondition references of the battle family (the only names options may carry)."""

from __future__ import annotations

# executor_ref (macros / atomics, see macros.py)
USE_MOVE = "battle.use_move"
USE_STRUGGLE = "battle.use_struggle"
SWITCH_TO = "battle.switch_to"
SEND_OUT = "battle.send_out"
USE_ITEM = "battle.use_item"
THROW_BALL = "battle.throw_ball"
ESCAPE = "battle.attempt_escape"
ADVANCE_TEXT = "battle.advance_text"
ACKNOWLEDGE = "battle.acknowledge"
BACK = "battle.back"
ANSWER = "battle.answer"
SAFARI_ACTION = "battle.safari_action"
CHOOSE_TARGET = "battle.choose_target"
FORGET_MOVE = "battle.forget_move"

# postcondition_ref (verifiers.py)
PC_MOVE = "battle.move_committed"
PC_SWITCH = "battle.switch_done"
PC_SENDOUT = "battle.sent_out"
PC_ITEM = "battle.item_used"
PC_BALL = "battle.ball_thrown"
PC_ESCAPE = "battle.escape_committed"
PC_TEXT = "battle.text_advanced"
PC_LEVELUP = "battle.level_up_acked"
PC_LEFT = "battle.menu_left"
PC_ANSWER = "battle.prompt_answered"
PC_SAFARI = "battle.safari_committed"
PC_TARGET = "battle.target_committed"
PC_MOVE_REPLACED = "battle.move_replaced"

#: prompt kinds of ``battle.answer`` (the `prompt` argument)
PROMPT_SWITCH_OFFER = 1
PROMPT_USE_NEXT = 2
PROMPT_LEARN_MOVE = 3
PROMPT_STOP_LEARNING = 4
PROMPT_NICKNAME = 5
