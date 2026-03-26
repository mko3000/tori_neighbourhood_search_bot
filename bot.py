import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.request import HTTPXRequest

import config
import tori_client

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
ADDRESS, RADIUS, CATEGORY, PRICE, BROWSING = range(5)

_RADIUS_OPTIONS = [1, 2, 5, 10, 20]


# ---------------------------------------------------------------------------
# /start — entry point
# ---------------------------------------------------------------------------

async def start(update: Update, context) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Hei! 👋  Send me an address to search Tori.fi listings nearby.\n\n"
        "Examples:\n"
        "• `Mannerheimintie 1, Helsinki`\n"
        "• `Tampere keskusta`\n"
        "• `Iso Roobertinkatu 14`\n\n"
        "Use /cancel to stop at any time.",
        parse_mode="Markdown",
    )
    return ADDRESS


# ---------------------------------------------------------------------------
# Step 1: receive address → ask for radius
# ---------------------------------------------------------------------------

async def handle_address(update: Update, context) -> int:
    address = update.message.text.strip()
    if len(address) < 3:
        await update.message.reply_text("Please send a more specific address.")
        return ADDRESS

    context.user_data["address_input"] = address

    keyboard = [[
        InlineKeyboardButton(f"{r} km", callback_data=f"radius:{r}")
        for r in _RADIUS_OPTIONS
    ]]
    await update.message.reply_text(
        f"Got it: *{address}*\n\nHow large a search radius?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return RADIUS


# ---------------------------------------------------------------------------
# Step 2: radius selected → geocode + fetch category overview
# ---------------------------------------------------------------------------

async def handle_radius(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()

    radius_km = float(query.data.split(":")[1])
    address = context.user_data["address_input"]

    await query.edit_message_text(
        f"Searching within *{int(radius_km)} km* of _{address}_…",
        parse_mode="Markdown",
    )

    try:
        bbox, location_name = await tori_client.resolve_address(address, radius_km)
    except ValueError as e:
        await query.edit_message_text(f"⚠️ {e}\nTry a different address.")
        return ADDRESS
    except Exception as e:
        logger.error("Geocoding error: %s", e)
        await query.edit_message_text("⚠️ Could not find that address. Please try again.")
        return ADDRESS

    try:
        result = await tori_client.search(bbox, location_name)
    except Exception as e:
        logger.error("Search error: %s", e)
        await query.edit_message_text("⚠️ Failed to fetch listings from Tori.fi. Please try again.")
        return ADDRESS

    context.user_data.update(
        bbox=bbox,
        location_name=location_name,
        radius_km=radius_km,
    )

    keyboard = [
        [InlineKeyboardButton(
            f"{cat.name}  ({cat.count:,})".replace(",", "\u202f"),
            callback_data=f"cat:{cat.code}"
        )]
        for cat in result.categories[:12]
    ]
    keyboard.append([InlineKeyboardButton("📋  All categories", callback_data="cat:ALL")])

    await query.edit_message_text(
        f"*{location_name}* · {int(radius_km)} km radius\n"
        f"{result.total:,} listings — pick a category:".replace(",", "\u202f"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return CATEGORY


# ---------------------------------------------------------------------------
# Step 3: category selected → ask for price range
# ---------------------------------------------------------------------------

async def handle_category(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()

    cat_code = query.data.split(":", 1)[1]
    context.user_data["category"] = cat_code if cat_code != "ALL" else None

    await query.edit_message_text(
        "Enter a price range (e.g. `50-500`) or send `any` for all prices:",
        parse_mode="Markdown",
    )
    return PRICE


# ---------------------------------------------------------------------------
# Step 4: price entered → fetch listings → show page 1
# ---------------------------------------------------------------------------

async def handle_price(update: Update, context) -> int:
    text = update.message.text.strip().lower()

    price_from = price_to = None
    price_label = "any price"

    if text != "any":
        m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", text)
        if not m:
            await update.message.reply_text(
                "Please enter a range like `50-500` or send `any`.",
                parse_mode="Markdown",
            )
            return PRICE
        price_from, price_to = int(m.group(1)), int(m.group(2))
        price_label = f"€{price_from}–{price_to}"

    context.user_data.update(price_from=price_from, price_to=price_to, price_label=price_label)

    msg = await update.message.reply_text("Fetching listings…")

    try:
        result = await tori_client.search(
            bbox=context.user_data["bbox"],
            location_name=context.user_data["location_name"],
            category=context.user_data.get("category"),
            price_from=price_from,
            price_to=price_to,
            page=1,
        )
    except Exception as e:
        logger.error("Search error: %s", e)
        await msg.edit_text("⚠️ Failed to fetch listings. Please try again.")
        return PRICE

    if not result.listings:
        await msg.edit_text(
            f"No listings found near *{result.location_name}* ({price_label}).\n"
            "Use /search to try again.",
            parse_mode="Markdown",
        )
        return ADDRESS

    await msg.delete()
    await _send_results(update.message.reply_text, result, context)
    return BROWSING


# ---------------------------------------------------------------------------
# Step 5: pagination / new search
# ---------------------------------------------------------------------------

async def handle_browsing(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "new_search":
        context.user_data.clear()
        await query.edit_message_text(
            "Send an address to start a new search:"
        )
        return ADDRESS

    page = int(query.data.split(":")[1])

    try:
        result = await tori_client.search(
            bbox=context.user_data["bbox"],
            location_name=context.user_data["location_name"],
            category=context.user_data.get("category"),
            price_from=context.user_data.get("price_from"),
            price_to=context.user_data.get("price_to"),
            page=page,
        )
    except Exception as e:
        logger.error("Pagination error: %s", e)
        await query.edit_message_text("⚠️ Failed to load page. Try again.")
        return BROWSING

    await _send_results(query.edit_message_text, result, context)
    return BROWSING


# ---------------------------------------------------------------------------
# Shared result renderer
# ---------------------------------------------------------------------------

async def _send_results(send_fn, result: tori_client.SearchResult, context) -> None:
    price_label = context.user_data.get("price_label", "any price")
    radius_km = int(context.user_data.get("radius_km", 0))

    lines = [
        f"*{result.location_name}* · {radius_km} km · {result.total:,} listings ({price_label})".replace(",", "\u202f"),
        f"_Page {result.current_page}/{result.last_page}_\n",
    ]

    for listing in result.listings:
        title = re.sub(r"([_*\[\]`])", r"\\\1", listing.title[:60])
        lines.append(f"• [{title}]({listing.url})\n  {listing.price_display} | {listing.location}")

    text = "\n".join(lines)

    nav_row = []
    if result.current_page > 1:
        nav_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page:{result.current_page - 1}"))
    if result.current_page < result.last_page:
        nav_row.append(InlineKeyboardButton("Next ➡", callback_data=f"page:{result.current_page + 1}"))

    keyboard = [nav_row] if nav_row else []
    keyboard.append([InlineKeyboardButton("🔍 New search", callback_data="new_search")])

    await send_fn(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

async def cancel(update: Update, context) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Use /start to begin again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    persistence = PicklePersistence(filepath="tori_bot.pkl")
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30)

    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .request(request)
        .persistence(persistence)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("search", start),
        ],
        states={
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address)],
            RADIUS: [CallbackQueryHandler(handle_radius, pattern=r"^radius:")],
            CATEGORY: [CallbackQueryHandler(handle_category, pattern=r"^cat:")],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price)],
            BROWSING: [CallbackQueryHandler(handle_browsing, pattern=r"^(page:\d+|new_search)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="tori_search",
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
