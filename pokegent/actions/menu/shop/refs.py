"""Stable shop scene, option and registry references."""

from __future__ import annotations

FAMILY = "shop"
SHOP_PROMPT = "dialog.shop_prompt"
MAIN = "shop.main"
BUY_LIST = "shop.buy_list"
BUY_QUANTITY = "shop.buy_quantity"
BUY_CONFIRM = "shop.buy_confirm"
BUY_DONE = "shop.buy_done"
SELL_LIST = "shop.sell_list"
SELL_QUANTITY = "shop.sell_quantity"
SELL_CONFIRM = "shop.sell_confirm"
SELL_DONE = "shop.sell_done"
CLAIMED_SCENES = frozenset(
    {
        SHOP_PROMPT,
        MAIN,
        BUY_LIST,
        BUY_QUANTITY,
        BUY_CONFIRM,
        BUY_DONE,
        SELL_LIST,
        SELL_QUANTITY,
        SELL_CONFIRM,
        SELL_DONE,
    }
)

OPT_BUY = "buy"
OPT_QUIT = "quit"
OPT_BACK = "back"
OPT_YES = "yes"
OPT_NO = "no"
OPT_CONTINUE = "continue_shopping"

REF_MAIN_CURSOR = "shop.main_cursor"
REF_PROMPT_CURSOR = "shop.prompt_cursor"
REF_LIST_ITEM = "shop.list_item"
REF_LIST_BACK = "shop.list_back"
REF_SELL_LIST_BACK = "shop.sell_list_back"
REF_QUANTITY = "shop.quantity"
REF_QUANTITY_BACK = "shop.quantity_back"
REF_CONFIRM = "shop.confirm"
REF_CONFIRM_NO = "shop.confirm_no"
REF_DONE_CONTINUE = "shop.done_continue"
REF_DONE_QUIT = "shop.done_quit"
REF_SELL_ITEM = "shop.sell_item"
REF_SELL_QUANTITY = "shop.sell_qty"
REF_SELL_CONFIRM = "shop.sell_yes"
REF_SELL_CONFIRM_NO = "shop.sell_no"
REF_SELL_DONE_CONTINUE = "shop.sell_done_continue"
REF_SELL_DONE_QUIT = "shop.sell_done_quit"

POST_MOVED_ON = "shop.moved_on"
POST_TRANSACTION = "shop.transaction"
POST_SELL_TRANSACTION = "shop.sell_transaction"
