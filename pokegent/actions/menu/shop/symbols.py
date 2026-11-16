"""Shop symbols and task-data layout facts for FireRed USA Rev0.

Function addresses are profile-owned and already appear in ``references/rom_profiles.yaml`` with
their decompile/oracle provenance. An unnamed callback or task is never guessed as a shop screen.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.rom.rom_data import RomProfile

BUY_CALLBACK = "CB2_BuyMenu"
SELL_CALLBACK = "CB2_BagMenuRun"
SELL_SETUP_CALLBACK_PTR = 0x08107F10
SELL_BAG_LOCATION = 2
SELL_BAG_TASK = "Task_BagMenu_HandleInput"
SELL_CONTEXT_TASK_PTR = 0x08108B8C
# ``Task_WaitPressAB_AfterSell`` (src/item_menu.c) observed as the owning task after
# ``Task_FinalizeSaleToShop`` has updated the bag and money in FireRed USA Rev0.
SELL_DONE_TASK_PTR = 0x0810AAF4
SHOP_TASK = "Task_ShopMenu"
RETURN_TASK = "Task_ReturnToShopMenu"
BUY_TASK = "Task_BuyMenu"
MESSAGE_TASK = "Task_ContinueTaskAfterMessagePrints"
QUANTITY_TASK = "Task_BuyHowManyDialogueHandleInput"
PURCHASE_DONE_TASK = "Task_ReturnToItemListAfterItemPurchase"
EXIT_BUY_TASK = "Task_ExitBuyMenu"
SELL_TASK = "Task_SellMenu"
CONFIRM_TASK = "Task_BuyConfirmInput"
# This task is not named by the shared UI reader yet. It is the Rev0 native
# confirm task observed after quantity A; keep the family-owned pointer here
# until the shared symbol catalogue can be extended without crossing family
# ownership.
CONFIRM_TASK_PTR = 0x080BF560

# ``src/shop.c`` task data, observed in the Rev0 quantity prompt:
# data[1] is the current quantity and data[5] is the selected item id.  The
# latter is deliberately read from the task record rather than inferred from
# the visible row: the visible list can be scrolled.
QUANTITY_TASK_DATA = 1
ITEM_TASK_DATA = 5
REQUIRED_SYMBOLS = (
    BUY_CALLBACK,
    SHOP_TASK,
    RETURN_TASK,
    BUY_TASK,
    QUANTITY_TASK,
    PURCHASE_DONE_TASK,
    EXIT_BUY_TASK,
    CONFIRM_TASK,
)


def missing_symbols(profile: RomProfile) -> tuple[str, ...]:
    return tuple(name for name in REQUIRED_SYMBOLS if not profile.has_symbol(name))
