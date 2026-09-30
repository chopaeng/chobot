"""
Chorder - Standalone ChOrder Bot Order Bot & Interactive Order Panel
Author: ChoPaeng
Features:
- /orderpanel: Deployable interactive Order Station in any channel with Web Builder link.
- Add to Cart & Cart Builder with 40-pocket slot limit.
- 16-Character Hex & Variant Encoding for ChOrder Bot order accuracy.
- Multi-page Catalog Search with interactive pagination (Prev/Next) & multi-select.
- My Orders: Synced with website database (order_bot_queue), live ETA, Dodo code, cancel button.
- Multi-stage DM Notification Engine (Ready, Completed, Cancelled) with restart deduplication.
- Live Queues: Real-time ChOrder Bot queue viewer with refresh.
- Presets: Official and curated bundles with 1-click loading and instant order.
- Quick Order: Direct support for both item names and raw space-separated hex strings.
"""

import asyncio
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Add parent directory to path if run standalone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import Config
from utils.database import connect_db
from utils.acnh_catalog import ACNHCatalog, CatalogItem, CatalogVillager, generate_full_item_hex
from utils.sysbot_api import SysBotClient, parse_order_input, _INVALID_DODO_CODES

logger = logging.getLogger("Chorder")

# ============================================================================
# CONSTANTS & DEBOUNCE LOCK
# ============================================================================
MAX_POCKET_SLOTS = 40
_submitting_users: Set[int] = set()

# ============================================================================
# CART DATA MODEL
# ============================================================================


class CartItem:
    def __init__(
        self,
        name: str,
        quantity: int = 1,
        category: str = "Items",
        image_url: str = "",
        variation: str = "",
        variant_id: str = "",
        internal_id: str = "",
        diy: bool = False,
    ):
        self.name = name
        self.quantity = max(1, quantity)
        self.category = category
        self.image_url = image_url
        self.variation = variation
        self.variant_id = variant_id
        self.internal_id = internal_id
        self.diy = diy

    @property
    def display_name(self) -> str:
        if self.variation and self.variation.lower() not in ("na", "none", ""):
            return f"{self.name} ({self.variation})"
        return self.name

    def to_hex_code(self) -> str:
        """Get the full 16-character / 4-character item hex code."""
        if self.internal_id:
            return generate_full_item_hex(
                base_id=self.internal_id,
                variant_string=self.variant_id or self.variation,
                category=self.category,
            )
        return ""

    def to_command_string(self) -> str:
        """
        Generate order string for this item.
        If hex code is available, repeats hex code for quantity.
        Otherwise falls back to display name.
        """
        hex_code = self.to_hex_code()
        if hex_code:
            if self.quantity > 1:
                return " ".join([hex_code] * self.quantity)
            return hex_code
        if self.quantity > 1:
            return f"{self.display_name} {self.quantity}"
        return self.display_name


class UserCart:
    """In-memory cart for an interactive user session."""
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.items: List[CartItem] = []
        self.villager: Optional[CatalogVillager] = None
        self.last_activity: float = time.time()

    def get_pocket_count(self) -> int:
        return sum(item.quantity for item in self.items)

    def is_full(self) -> bool:
        return self.get_pocket_count() >= MAX_POCKET_SLOTS

    def add_item(
        self,
        name: str,
        quantity: int = 1,
        category: str = "Items",
        image_url: str = "",
        variation: str = "",
        variant_id: str = "",
        internal_id: str = "",
        diy: bool = False,
    ) -> bool:
        self.last_activity = time.time()
        current_count = self.get_pocket_count()
        if current_count >= MAX_POCKET_SLOTS:
            return False

        qty_to_add = min(quantity, MAX_POCKET_SLOTS - current_count)
        if qty_to_add <= 0:
            return False

        # If duplicate item exists, update quantity
        for existing in self.items:
            if (
                existing.name.lower() == name.lower()
                and existing.variation.lower() == variation.lower()
                and existing.internal_id == internal_id
            ):
                existing.quantity += qty_to_add
                return True

        self.items.append(
            CartItem(
                name=name,
                quantity=qty_to_add,
                category=category,
                image_url=image_url,
                variation=variation,
                variant_id=variant_id,
                internal_id=internal_id,
                diy=diy,
            )
        )
        return True

    def set_villager(self, villager: CatalogVillager) -> None:
        self.last_activity = time.time()
        self.villager = villager

    def remove_villager(self) -> None:
        self.last_activity = time.time()
        self.villager = None

    def remove_item(self, index: int) -> bool:
        self.last_activity = time.time()
        if 0 <= index < len(self.items):
            self.items.pop(index)
            return True
        return False

    def clear(self) -> None:
        self.last_activity = time.time()
        self.items.clear()
        self.villager = None

    def to_order_string(self) -> str:
        """Build ChOrder Bot order command string."""
        item_tokens = []
        for item in self.items:
            tok = item.to_command_string()
            if tok:
                item_tokens.append(tok)

        villager_part = f"villager:{self.villager.villager_id or self.villager.name}" if self.villager else None

        # Check if all tokens are pure hex codes and spaces
        all_hex = all(
            bool(re.match(r"^[0-9A-Fa-f\s]+$", tok)) for tok in item_tokens
        ) if item_tokens else False

        if all_hex:
            items_str = " ".join(item_tokens)
            if villager_part:
                return f"{items_str} {villager_part}".strip()
            return items_str
        else:
            if villager_part:
                item_tokens.append(villager_part)
            return ", ".join(item_tokens)


class CartManager:
    """Manages active user carts and handles inactivity eviction."""
    def __init__(self):
        self._carts: Dict[int, UserCart] = {}

    def get_cart(self, user_id: int) -> UserCart:
        if user_id not in self._carts:
            self._carts[user_id] = UserCart(user_id)
        self._carts[user_id].last_activity = time.time()
        return self._carts[user_id]

    def cleanup_stale_carts(self, max_age_seconds: float = 7200) -> int:
        """Evict carts inactive for more than max_age_seconds (default 2 hours)."""
        now = time.time()
        stale_keys = [uid for uid, c in self._carts.items() if (now - c.last_activity) > max_age_seconds]
        for uid in stale_keys:
            del self._carts[uid]
        if stale_keys:
            logger.info(f"[CartManager] Evicted {len(stale_keys)} inactive carts.")
        return len(stale_keys)


cart_manager = CartManager()
catalog = ACNHCatalog.get_instance()
sysbot = SysBotClient()

# ============================================================================
# EMBED FACTORIES & UI HELPERS
# ============================================================================
EMBED_COLOR_DEFAULT = 0x2ECC71  # ACNH Green
EMBED_COLOR_ORDER = 0x3498DB    # Blue
EMBED_COLOR_WARN = 0xE67E22     # Orange
EMBED_COLOR_ERROR = 0xE74C3C    # Red


def resolve_guild_icon(target_or_interaction: Any, bot: Optional[Any] = None) -> Optional[str]:
    """
    Safely extract a static PNG guild icon URL that renders properly in Discord
    embed footers and thumbnails, avoiding broken animated GIF footer rendering.
    """
    guild = None
    if hasattr(target_or_interaction, "icon") and target_or_interaction.icon:
        guild = target_or_interaction
    elif hasattr(target_or_interaction, "guild") and target_or_interaction.guild:
        guild = target_or_interaction.guild
    elif hasattr(target_or_interaction, "channel") and hasattr(target_or_interaction.channel, "guild"):
        guild = target_or_interaction.channel.guild

    if not guild and bot and getattr(Config, "GUILD_ID", None):
        guild = bot.get_guild(Config.GUILD_ID)

    if guild and guild.icon:
        try:
            return guild.icon.with_format("png").url
        except Exception:
            return guild.icon.url

    return getattr(Config, "DEFAULT_PFP", None)


def apply_chopaeng_footer(embed: discord.Embed, guild_icon: Optional[str] = None):
    """Apply the signature ChoPaeng Camp footer and animated line divider."""
    if getattr(Config, "FOOTER_LINE", None):
        embed.set_image(url=Config.FOOTER_LINE)
    icon = guild_icon or getattr(Config, "DEFAULT_PFP", None) or "https://nh-cdn.catalogue.ac/NpcIcon/cat23.png"
    embed.set_footer(text="Chopaeng Camp™", icon_url=icon)


def add_chunked_fields(
    embed: discord.Embed,
    field_title: str,
    lines: List[str],
    max_chars: int = 1000,
    inline: bool = False,
):
    """Safely split long lists into embed fields avoiding Discord's 1024-character field limit."""
    if not lines:
        return
    chunks = []
    current_chunk: List[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    for idx, chunk in enumerate(chunks):
        title = field_title if idx == 0 else f"{field_title} (Cont.)"
        embed.add_field(name=title, value=chunk, inline=inline)


def build_panel_embed(island_name: str = "Sinta", is_online: bool = True, guild_icon: Optional[str] = None) -> discord.Embed:
    """Build the main Order Station dashboard embed."""
    status_emoji = "🟢" if is_online else "🔴"
    status_text = "Online & Accepting Orders" if is_online else "Offline / Maintenance"

    embed = discord.Embed(
        title=f"{Config.EMOJI_SEARCH} ChOrder Station",
        description=(
            f"Welcome to the **ChOrder Bot Order Terminal**! Build custom inventory pockets, "
            f"order dream villagers, and enjoy automated fast delivery directly to your island.\n\n"
            f"**Bot Status:** {status_emoji} `{status_text}`\n"
            f"**Active Island:** 🏝️ `{island_name}`\n"
            f"**Pocket Capacity:** 🎒 `Max 40 Slots` + `1 Villager Plot`\n"
        ),
        color=EMBED_COLOR_DEFAULT if is_online else EMBED_COLOR_WARN,
    )
    embed.add_field(
        name=f"{Config.STAR_PINK} Quick Guide",
        value=(
            "• **Add to Cart / My Cart:** Manage your 40 inventory pocket slots.\n"
            "• **Search Catalog:** Search items & villagers with multi-page navigation and images.\n"
            "• **Presets / Bundles:** Load curated item sets in 1 click or instant order.\n"
            "• **My Orders:** View real-time queue position, ETA, and your Dodo Code.\n"
            "• **Live Queue:** Check current ChOrder Bot island activity.\n"
            "• **Quick Order:** Paste item lists or direct hex codes.\n"
            "• **Open Web Builder:** Visual drag-and-drop pocket grid on the web."
        ),
        inline=False,
    )
    if guild_icon:
        embed.set_author(name="ChoPaeng Camp", icon_url=guild_icon)
        embed.set_thumbnail(url=guild_icon)
    else:
        embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
    apply_chopaeng_footer(embed, guild_icon)
    return embed


def build_cart_embed(cart: UserCart, user: Any, guild_icon: Optional[str] = None) -> discord.Embed:
    """Build the interactive Cart viewer embed."""
    slots_used = cart.get_pocket_count()
    slots_left = MAX_POCKET_SLOTS - slots_used
    uname = getattr(user, "display_name", getattr(user, "name", "Your"))

    # Visual pocket bar
    bar_filled = int((slots_used / MAX_POCKET_SLOTS) * 12)
    bar_empty = 12 - bar_filled
    bar_str = "█" * bar_filled + "░" * bar_empty

    embed = discord.Embed(
        title=f"{Config.EMOJI_SEARCH} {uname}'s Cart & Pocket Builder",
        description=(
            f"**Pocket Capacity:** `[{bar_str}]` **{slots_used} / {MAX_POCKET_SLOTS} slots** "
            f"({slots_left} remaining)\n"
        ),
        color=EMBED_COLOR_ORDER,
    )

    if cart.villager:
        embed.add_field(
            name=f"{Config.STAR_PINK} Selected Villager (Move-in Plot)",
            value=f"**{cart.villager.name}** (`ID: {cart.villager.villager_id}`) • *{cart.villager.species} / {cart.villager.personality}*",
            inline=False,
        )
        if cart.villager.photo_url:
            embed.set_thumbnail(url=cart.villager.photo_url)
    elif cart.items and cart.items[0].image_url:
        embed.set_thumbnail(url=cart.items[0].image_url)

    if not cart.items and not cart.villager:
        embed.add_field(
            name=f"{Config.STAR_PINK} Cart is Empty",
            value="Your cart is currently empty! Use **Browse Catalog** or **Add Item** below to begin.",
            inline=False,
        )
    else:
        # Group items into lines
        lines = []
        for i, item in enumerate(cart.items, 1):
            qty_str = f" x{item.quantity}" if item.quantity > 1 else ""
            cat_badge = f"[{item.category}]"
            lines.append(f"`{i:02d}.` **{item.display_name}**{qty_str} *{cat_badge}*")

        # Safely chunk fields under 1024 characters
        add_chunked_fields(embed, f"{Config.STAR_PINK} Pocket Items", lines, max_chars=950)

    order_cmd = cart.to_order_string()
    if order_cmd:
        display_cmd = order_cmd if len(order_cmd) <= 900 else f"{order_cmd[:900]}..."
        embed.add_field(
            name=f"{Config.STAR_PINK} ChOrder Bot Command String",
            value=f"```!order {display_cmd}```",
            inline=False,
        )

    apply_chopaeng_footer(embed, guild_icon)
    return embed


# ============================================================================
# DISCORD UI MODALS & VIEWS
# ============================================================================


class SingleActionCloseView(discord.ui.View):
    """Simple view with a single 'Close' button to dismiss confirmation embeds."""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌")
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass


class QuickOrderModal(discord.ui.Modal, title="⚡ Quick ChOrder Bot Order"):
    order_input = discord.ui.TextInput(
        label="Items, Hex Codes, or Villager",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Gold nugget 30, Royal crown 10, villager:Raymond\nor 14BB 16DB 0000002000003604 villager:cat23",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw_text = self.order_input.value.strip()
        if not raw_text:
            await interaction.followup.send("❌ Please enter valid items, hex codes, or a villager to order.", ephemeral=True)
            return

        parsed_order, parsed_villager = parse_order_input(raw_text)
        if not parsed_order and not parsed_villager:
            await interaction.followup.send("❌ Please enter valid items, hex codes, or a villager to order.", ephemeral=True)
            return

        # Check if input is a sequence of pure hex tokens
        tokens = parsed_order.split()
        is_raw_hex = all(
            bool(re.match(r"^[0-9A-Fa-f]{4}$|^[0-9A-Fa-f]{16}$", tok)) for tok in tokens
        ) if tokens else False

        if is_raw_hex:
            final_order_text = parsed_order
            if parsed_villager:
                final_order_text += f" villager:{parsed_villager}"
        else:
            final_order_text = raw_text

        user_id_int = interaction.user.id
        if user_id_int in _submitting_users:
            await interaction.followup.send("⏳ An order submission is already in progress. Please wait a moment!", ephemeral=True)
            return

        _submitting_users.add(user_id_int)
        try:
            username = interaction.user.display_name
            user_id = str(user_id_int)

            result = await sysbot.submit_order(
                username=username,
                order_text=final_order_text,
                user_id=user_id,
            )

            if result.get("success") or result.get("order_id"):
                order_id = result.get("order_id", "Unknown")
                pos = result.get("queue_position", 1)
                eta = result.get("estimated_minutes", 2)
                island = result.get("island_name", "Sinta")
                guild_icon = interaction.guild.icon.url if (interaction.guild and interaction.guild.icon) else None

                embed = discord.Embed(
                    title="✅ Order Submitted to ChOrder Bot!",
                    description=(
                        f"Your order has been placed into the ChOrder Bot queue.\n\n"
                        f"**Order ID:** `{order_id}`\n"
                        f"**Queue Position:** `#{pos}`\n"
                        f"**Estimated Time:** `~{eta} min`\n"
                        f"**Island:** 🏝️ `{island}`\n\n"
                        f"**Order Payload:**\n```{final_order_text[:900]}```\n"
                        f"You will receive a **direct message** with your **Dodo Code** when the bot is ready. "
                        f"Track this anytime via the **My Orders** button!"
                    ),
                    color=EMBED_COLOR_DEFAULT,
                )
                apply_chopaeng_footer(embed, guild_icon)
                await interaction.followup.send(embed=embed, view=SingleActionCloseView(), ephemeral=True)
            else:
                err_msg = result.get("error") or "ChOrder Bot could not process this order."
                await interaction.followup.send(f"❌ **Failed to submit order:** {err_msg}", ephemeral=True)
        except Exception as exc:
            logger.exception(f"[QuickOrderModal] Error submitting order: {exc}")
            await interaction.followup.send("❌ An unexpected error occurred while communicating with ChOrder Bot. Please try again.", ephemeral=True)
        finally:
            _submitting_users.discard(user_id_int)


class AddItemModal(discord.ui.Modal, title="➕ Add Item to Cart"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Gold nugget, Royal crown, Pisces lamp",
        required=True,
        max_length=100,
    )
    quantity = discord.ui.TextInput(
        label="Quantity / Count (Max 30)",
        placeholder="1",
        default="1",
        required=False,
        max_length=3,
    )

    def __init__(self, cart: UserCart, guild_icon: Optional[str] = None):
        super().__init__()
        self.cart = cart
        self.guild_icon = guild_icon

    async def on_submit(self, interaction: discord.Interaction):
        name_str = self.item_name.value.strip()
        qty_str = self.quantity.value.strip() or "1"
        try:
            qty = max(1, min(30, int(qty_str)))
        except ValueError:
            qty = 1

        # Check catalog for image/details
        item_data = catalog.get_item(name_str)
        if not item_data:
            matches = catalog.search_items(name_str, limit=3)
            if matches:
                suggestions = ", ".join([f"**{m.name}**" for m in matches])
                await interaction.response.send_message(
                    f"❌ Could not find exact item '{name_str}'. Did you mean: {suggestions}?\n"
                    f"Please enter the exact name or use **Browse Catalog** to pick with guaranteed hex encoding.",
                    ephemeral=True,
                )
                return
            else:
                await interaction.response.send_message(
                    f"❌ Item '{name_str}' was not found in the ACNH catalog. Use **Browse Catalog** to search.",
                    ephemeral=True,
                )
                return

        added = self.cart.add_item(
            name=item_data.name,
            quantity=qty,
            category=item_data.category,
            image_url=item_data.image_url,
            variation=item_data.variation,
            variant_id=item_data.variant_id,
            internal_id=item_data.internal_id,
            diy=item_data.diy,
        )

        if not added:
            await interaction.response.send_message(
                "❌ Cart is full! Maximum 40 pocket slots reached.", ephemeral=True
            )
            return

        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)


class SetVillagerModal(discord.ui.Modal, title="🏡 Set Villager for Move-in Plot"):
    villager_query = discord.ui.TextInput(
        label="Villager Name or ID",
        placeholder="e.g. Raymond, Marshal, brd09, cat23",
        required=True,
        max_length=50,
    )

    def __init__(self, cart: UserCart, guild_icon: Optional[str] = None):
        super().__init__()
        self.cart = cart
        self.guild_icon = guild_icon

    async def on_submit(self, interaction: discord.Interaction):
        query = self.villager_query.value.strip()
        villager = catalog.get_villager(query)

        if not villager:
            matches = catalog.search_villagers(query, limit=3)
            if matches:
                suggestions = ", ".join([f"**{m.name}** (`{m.villager_id}`)" for m in matches])
                await interaction.response.send_message(
                    f"❌ No exact villager found for '{query}'. Did you mean: {suggestions}?\n"
                    f"Please enter their exact name or ID.",
                    ephemeral=True,
                )
                return
            else:
                await interaction.response.send_message(
                    f"❌ No villager found matching '{query}'. Use **Search Catalog** to browse orderable villagers.",
                    ephemeral=True,
                )
                return

        self.cart.set_villager(villager)
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)


class CatalogSearchModal(discord.ui.Modal, title="🔍 Search ACNH Catalog"):
    search_query = discord.ui.TextInput(
        label="Search Keyword (Items & Villagers)",
        placeholder="e.g. painting, crown, diy, Raymond, gold",
        required=True,
        max_length=60,
    )

    def __init__(self, cart: UserCart, guild_icon: Optional[str] = None):
        super().__init__()
        self.cart = cart
        self.guild_icon = guild_icon

    async def on_submit(self, interaction: discord.Interaction):
        query = self.search_query.value.strip()
        items = catalog.search_items(query, limit=100)
        villagers = catalog.search_villagers(query, limit=10)

        view = CatalogSearchView(self.cart, query=query, items=items, villagers=villagers, page=0, guild_icon=self.guild_icon)
        embed = view.build_search_embed()

        # If modal is invoked on an existing ephemeral message, edit in place
        if interaction.message:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ── Catalog Search View (with Multi-Page Pagination & Multi-Select) ────────────


class CatalogSearchView(discord.ui.View):
    """Catalog search results view with interactive pagination, multi-select, and images."""

    def __init__(
        self,
        cart: UserCart,
        query: str = "",
        category: Optional[str] = None,
        items: Optional[List[CatalogItem]] = None,
        villagers: Optional[List[CatalogVillager]] = None,
        page: int = 0,
        guild_icon: Optional[str] = None,
    ):
        super().__init__(timeout=300)
        self.cart = cart
        self.query = query
        self.category = category
        self.items = items if items is not None else catalog.search_items(query, limit=100, category=category)
        self.villagers = villagers if villagers is not None else catalog.search_villagers(query, limit=10)
        self.page = page
        self.page_size = 15
        self.guild_icon = guild_icon
        self.message: Optional[discord.Message] = None
        self._build_components()

    @property
    def total_pages(self) -> int:
        if not self.items:
            return 1
        return max(1, (len(self.items) + self.page_size - 1) // self.page_size)

    def get_current_page_items(self) -> List[CatalogItem]:
        start = self.page * self.page_size
        return self.items[start : start + self.page_size]

    def _build_components(self):
        self.clear_items()
        current_items = self.get_current_page_items()

        # Multi-select dropdown for items on the current page
        if current_items:
            options = []
            for idx, it in enumerate(current_items):
                desc = f"[{it.category}]"
                if it.variation:
                    desc += f" Var: {it.variation}"
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=desc[:100],
                        value=f"item_{idx}",
                        emoji="📦" if not it.diy else "📜",
                    )
                )

            max_opts = min(len(options), 10)
            item_select = discord.ui.Select(
                placeholder=f"Select items (Page {self.page+1}/{self.total_pages}, multi-select up to {max_opts})...",
                min_values=1,
                max_values=max_opts,
                options=options,
                custom_id="catalog_multi_select",
                row=0,
            )
            item_select.callback = self.on_item_select
            self.add_item(item_select)

        # Dropdown for villagers (shown on first page if matches exist)
        if self.villagers and self.page == 0:
            v_options = []
            for idx, v in enumerate(self.villagers[:10]):
                v_options.append(
                    discord.SelectOption(
                        label=v.display_name[:100],
                        description=f"ID: {v.villager_id} • {v.personality}"[:100],
                        value=f"villager_{idx}",
                        emoji="🐱",
                    )
                )

            villager_select = discord.ui.Select(
                placeholder="Select a villager for your move-in plot...",
                min_values=1,
                max_values=1,
                options=v_options,
                custom_id="villager_select",
                row=1,
            )
            villager_select.callback = self.on_villager_select
            self.add_item(villager_select)

        # Action and Navigation buttons
        nav_row = 1 if not (self.villagers and self.page == 0) else 2

        btn_prev = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page <= 0),
            row=nav_row,
        )
        btn_prev.callback = self.on_prev_page
        self.add_item(btn_prev)

        btn_next = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1),
            row=nav_row,
        )
        btn_next.callback = self.on_next_page
        self.add_item(btn_next)

        btn_search = discord.ui.Button(label="New Search", style=discord.ButtonStyle.primary, emoji="🔍", row=nav_row)
        btn_search.callback = self.on_search_button
        self.add_item(btn_search)

        btn_cart = discord.ui.Button(
            label=f"View Cart ({self.cart.get_pocket_count()}/40)",
            style=discord.ButtonStyle.secondary,
            emoji="🛒",
            row=nav_row,
        )
        btn_cart.callback = self.on_view_cart
        self.add_item(btn_cart)

        btn_close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌", row=nav_row)
        btn_close.callback = self.on_close
        self.add_item(btn_close)

    async def on_prev_page(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self._build_components()
            embed = self.build_search_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_next_page(self, interaction: discord.Interaction):
        if self.page < self.total_pages - 1:
            self.page += 1
            self._build_components()
            embed = self.build_search_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_close(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def build_search_embed(self) -> discord.Embed:
        current_items = self.get_current_page_items()
        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} Catalog Search: '{self.query or 'All'}'",
            description=(
                f"Found **{len(self.items)}** items and **{len(self.villagers)}** villagers.\n"
                f"**Page:** `{self.page + 1} / {self.total_pages}`\n"
                f"Use the **dropdowns below** to multi-select items into your cart!\n"
            ),
            color=EMBED_COLOR_DEFAULT,
        )

        if self.villagers and self.villagers[0].photo_url:
            embed.set_thumbnail(url=self.villagers[0].photo_url)
        elif current_items and current_items[0].image_url:
            embed.set_thumbnail(url=current_items[0].image_url)

        if current_items:
            lines = []
            start_num = self.page * self.page_size + 1
            for i, it in enumerate(current_items, start_num):
                diy_badge = " *(DIY)*" if it.diy else ""
                lines.append(f"`{i:02d}.` **{it.display_name}**{diy_badge} — `{it.category}`")
            add_chunked_fields(embed, f"{Config.STAR_PINK} Matching Items (Page {self.page + 1}/{self.total_pages})", lines, max_chars=950)

        if self.villagers and self.page == 0:
            v_lines = []
            for v in self.villagers[:5]:
                v_lines.append(f"• **{v.name}** (`ID: {v.villager_id}`) — *{v.species} / {v.personality}*")
            add_chunked_fields(embed, f"{Config.STAR_PINK} Matching Villagers (Move-in Plot)", v_lines, max_chars=950)

        if not self.items and not self.villagers:
            embed.description = "❌ No items or villagers found matching that query. Try another keyword!"

        apply_chopaeng_footer(embed, self.guild_icon)
        return embed

    async def on_item_select(self, interaction: discord.Interaction):
        selected_values = interaction.data.get("values", [])
        current_items = self.get_current_page_items()
        added_names = []
        for val in selected_values:
            idx = int(val.replace("item_", ""))
            if idx < len(current_items):
                it = current_items[idx]
                if self.cart.add_item(
                    name=it.name,
                    quantity=1,
                    category=it.category,
                    image_url=it.image_url,
                    variation=it.variation,
                    variant_id=it.variant_id,
                    internal_id=it.internal_id,
                    diy=it.diy,
                ):
                    added_names.append(it.display_name)

        self._build_components()
        embed = self.build_search_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_villager_select(self, interaction: discord.Interaction):
        selected = interaction.data.get("values", [""])[0]
        idx = int(selected.replace("villager_", ""))
        if idx < len(self.villagers):
            v = self.villagers[idx]
            self.cart.set_villager(v)

            self._build_components()
            embed = self.build_search_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_search_button(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CatalogSearchModal(self.cart, self.guild_icon))

    async def on_view_cart(self, interaction: discord.Interaction):
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Presets / Bundles View ───────────────────────────────────────────────────


class BundlesView(discord.ui.View):
    """Presets and bundles browser with full multi-field support and safe embed chunking."""

    def __init__(self, cart: UserCart, bundles: List[Dict[str, Any]], guild_icon: Optional[str] = None):
        super().__init__(timeout=300)
        self.cart = cart
        self.bundles = bundles
        self.guild_icon = guild_icon
        self.selected_bundle: Optional[Dict[str, Any]] = None
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self):
        self.clear_items()

        if self.bundles:
            options = []
            for idx, b in enumerate(self.bundles[:25]):
                name = b.get("name") or b.get("title") or f"Bundle {idx+1}"
                category = b.get("category", "General")
                items_list = b.get("orderItems") or b.get("items") or []
                items_cnt = len(items_list)
                is_selected = (self.selected_bundle == b)
                options.append(
                    discord.SelectOption(
                        label=name[:100],
                        description=f"[{category}] {items_cnt} items"[:100],
                        value=f"bundle_{idx}",
                        default=is_selected,
                        emoji="📦",
                    )
                )

            select = discord.ui.Select(
                placeholder="Choose a preset bundle from the list...",
                options=options,
                custom_id="bundle_select",
                row=0,
            )
            select.callback = self.on_bundle_select
            self.add_item(select)

        # Buttons (Disabled until a bundle is selected)
        has_selection = self.selected_bundle is not None

        btn_load = discord.ui.Button(
            label="Load Bundle to Cart",
            style=discord.ButtonStyle.primary,
            disabled=not has_selection,
            emoji="🛒",
            row=1,
        )
        btn_load.callback = self.on_load_cart
        self.add_item(btn_load)

        btn_order = discord.ui.Button(
            label="Instant Order Bundle",
            style=discord.ButtonStyle.success,
            disabled=not has_selection,
            emoji="🚀",
            row=1,
        )
        btn_order.callback = self.on_instant_order
        self.add_item(btn_order)

        btn_cart = discord.ui.Button(
            label=f"View Cart ({self.cart.get_pocket_count()}/40)",
            style=discord.ButtonStyle.secondary,
            emoji="🎒",
            row=1,
        )
        btn_cart.callback = self.on_view_cart
        self.add_item(btn_cart)

        btn_close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌", row=1)
        btn_close.callback = self.on_close
        self.add_item(btn_close)

    async def on_close(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def build_bundle_embed(self) -> discord.Embed:
        if not self.selected_bundle:
            embed = discord.Embed(
                title=f"{Config.EMOJI_SEARCH} Preset Bundles & Curated Sets",
                description=(
                    "Browse official and popular pre-configured item sets!\n\n"
                    "👉 **Please choose a preset bundle from the dropdown menu below** to preview its included items, "
                    "load them into your cart, or place an instant order."
                ),
                color=EMBED_COLOR_ORDER,
            )
            if self.bundles:
                lines = []
                for i, b in enumerate(self.bundles[:10], 1):
                    name = b.get("name") or b.get("title") or f"Bundle {i}"
                    cat = b.get("category") or "General"
                    cnt = len(b.get("orderItems") or b.get("items") or [])
                    lines.append(f"`{i:02d}.` **{name}** — *[{cat}] ({cnt} items)*")
                add_chunked_fields(embed, f"{Config.STAR_PINK} Available Presets", lines, max_chars=950)

            if self.guild_icon:
                embed.set_author(name="ChoPaeng Camp", icon_url=self.guild_icon)
                embed.set_thumbnail(url=self.guild_icon)
            else:
                embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
            apply_chopaeng_footer(embed, self.guild_icon)
            return embed

        b = self.selected_bundle
        name = b.get("name") or b.get("title") or "Preset Bundle"
        desc = b.get("description") or "Official pre-configured ACNH item set."
        category = b.get("category", "Popular")
        items = b.get("orderItems") or b.get("items") or []

        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} Preset: {name}",
            description=f"**Category:** `{category}`\n{desc}\n\n**Included Items ({len(items)}):**",
            color=EMBED_COLOR_DEFAULT,
        )

        if items:
            first_img = items[0].get("image") or items[0].get("imageUrl")
            if first_img:
                embed.set_thumbnail(url=first_img)

            lines = []
            for i, it in enumerate(items, 1):
                it_name = it.get("name") or "Unknown Item"
                qty = it.get("quantity", 1)
                qty_str = f" x{qty}" if qty > 1 else ""
                var_str = f" ({it.get('variantLabel') or it.get('variation')})" if (it.get('variantLabel') or it.get('variation')) else ""
                lines.append(f"`{i:02d}.` **{it_name}**{var_str}{qty_str}")

            add_chunked_fields(embed, f"{Config.STAR_PINK} Item Breakdown", lines, max_chars=950)

        apply_chopaeng_footer(embed, self.guild_icon)
        return embed

    async def on_bundle_select(self, interaction: discord.Interaction):
        val = interaction.data.get("values", [""])[0]
        idx = int(val.replace("bundle_", ""))
        if idx < len(self.bundles):
            self.selected_bundle = self.bundles[idx]
            self._build_components()
            embed = self.build_bundle_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_load_cart(self, interaction: discord.Interaction):
        if not self.selected_bundle:
            await interaction.response.send_message("❌ No bundle selected.", ephemeral=True)
            return

        items = self.selected_bundle.get("orderItems") or self.selected_bundle.get("items") or []
        added_count = 0
        for it in items:
            name = it.get("name")
            qty = it.get("quantity", 1)
            cat = it.get("category", "Items")
            img = it.get("image") or it.get("imageUrl") or ""
            item_id = it.get("itemId") or it.get("id") or ""
            variant_id = it.get("variantId") or ""
            var_label = it.get("variantLabel") or it.get("variation") or ""
            if name and self.cart.add_item(
                name=name,
                quantity=qty,
                category=cat,
                image_url=img,
                variation=var_label,
                variant_id=variant_id,
                internal_id=item_id,
            ):
                added_count += 1

        self._build_components()
        embed = self.build_bundle_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_instant_order(self, interaction: discord.Interaction):
        if not self.selected_bundle:
            await interaction.response.send_message("❌ No bundle selected.", ephemeral=True)
            return

        user_id_int = interaction.user.id
        if user_id_int in _submitting_users:
            await interaction.response.send_message("⏳ An order submission is already in progress. Please wait a moment!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _submitting_users.add(user_id_int)
        try:
            items = self.selected_bundle.get("orderItems") or self.selected_bundle.get("items") or []
            order_parts = []
            for it in items:
                name = it.get("name")
                qty = it.get("quantity", 1)
                item_id = it.get("itemId") or it.get("id") or ""
                variant_id = it.get("variantId") or ""
                cat = it.get("category", "")
                if item_id:
                    hex_code = generate_full_item_hex(item_id, variant_id, cat)
                    if hex_code:
                        order_parts.extend([hex_code] * qty)
                        continue
                if name:
                    order_parts.append(f"{name} {qty}" if qty > 1 else name)

            all_hex = all(bool(re.match(r"^[0-9A-Fa-f\s]+$", tok)) for tok in order_parts) if order_parts else False
            raw_cmd = " ".join(order_parts) if all_hex else ", ".join(order_parts)

            result = await sysbot.submit_order(
                username=interaction.user.display_name,
                order_text=raw_cmd,
                user_id=str(user_id_int),
            )

            if result.get("success") or result.get("order_id"):
                bundle_name = self.selected_bundle.get("name") or self.selected_bundle.get("title") or "Bundle"
                order_id = result.get("order_id", "Unknown")
                pos = result.get("queue_position", 1)
                eta = result.get("estimated_minutes", 2)
                island = result.get("island_name", "Sinta")
                embed = discord.Embed(
                    title="✅ Bundle Ordered!",
                    description=(
                        f"**Bundle:** `{bundle_name}`\n"
                        f"**Order ID:** `{order_id}`\n"
                        f"**Queue Position:** `#{pos}`\n"
                        f"**Estimated Arrival:** `~{eta} min`\n"
                        f"**Island:** 🏝️ `{island}`\n\n"
                        f"Track your order status and Dodo Code anytime under **My Orders**."
                    ),
                    color=EMBED_COLOR_DEFAULT,
                )
                apply_chopaeng_footer(embed, self.guild_icon)
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=SingleActionCloseView())
            else:
                err = result.get("error") or "Order could not be submitted."
                await interaction.followup.send(f"❌ **Failed to order bundle:** {err}", ephemeral=True)
        except Exception as exc:
            logger.exception(f"[BundlesView] Error instant ordering: {exc}")
            await interaction.followup.send("❌ An unexpected error occurred while placing the order. Please try again.", ephemeral=True)
        finally:
            _submitting_users.discard(user_id_int)

    async def on_view_cart(self, interaction: discord.Interaction):
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Cart View & Clear Confirmation View ──────────────────────────────────────


class ClearCartConfirmView(discord.ui.View):
    """Confirmation view to avoid accidental cart wipes."""

    def __init__(self, cart: UserCart, guild_icon: Optional[str] = None):
        super().__init__(timeout=60)
        self.cart = cart
        self.guild_icon = guild_icon
        self.message: Optional[discord.Message] = None

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(label="Yes, Clear Everything", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def btn_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cart.clear()
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        view = CartView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=view)


class CartView(discord.ui.View):
    """Interactive Cart Management View."""

    def __init__(self, cart: UserCart, guild_icon: Optional[str] = None):
        super().__init__(timeout=300)
        self.cart = cart
        self.guild_icon = guild_icon
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self):
        self.clear_items()

        # If items exist, add remove select menu
        if self.cart.items:
            options = []
            for i, it in enumerate(self.cart.items[:25]):
                options.append(
                    discord.SelectOption(
                        label=f"{i+1}. {it.display_name}"[:100],
                        description=f"Qty: {it.quantity} • {it.category}"[:100],
                        value=f"rem_{i}",
                        emoji="🗑️",
                    )
                )
            rem_select = discord.ui.Select(
                placeholder="Remove an item from cart...",
                options=options,
                custom_id="cart_remove_select",
                row=0,
            )
            rem_select.callback = self.on_remove_item
            self.add_item(rem_select)

        # Row 1 Buttons
        btn_submit = discord.ui.Button(
            label="Submit Order to ChOrder Bot",
            style=discord.ButtonStyle.success,
            emoji="🚀",
            row=1,
        )
        btn_submit.callback = self.on_submit_order
        self.add_item(btn_submit)

        btn_add = discord.ui.Button(
            label="Add Item",
            style=discord.ButtonStyle.primary,
            emoji="➕",
            row=1,
        )
        btn_add.callback = self.on_add_item
        self.add_item(btn_add)

        btn_villager = discord.ui.Button(
            label="Set Villager",
            style=discord.ButtonStyle.secondary,
            emoji="🐱",
            row=1,
        )
        btn_villager.callback = self.on_set_villager
        self.add_item(btn_villager)

        # Row 2 Buttons
        btn_search = discord.ui.Button(
            label="Browse Catalog",
            style=discord.ButtonStyle.secondary,
            emoji="🔍",
            row=2,
        )
        btn_search.callback = self.on_browse_catalog
        self.add_item(btn_search)

        btn_clear = discord.ui.Button(
            label="Clear Cart",
            style=discord.ButtonStyle.danger,
            emoji="🧹",
            row=2,
        )
        btn_clear.callback = self.on_clear_cart
        self.add_item(btn_clear)

        btn_close = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.secondary,
            emoji="❌",
            row=2,
        )
        btn_close.callback = self.on_close
        self.add_item(btn_close)

    async def on_close(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def on_submit_order(self, interaction: discord.Interaction):
        if not self.cart.items and not self.cart.villager:
            await interaction.response.send_message("❌ Cart is empty! Add items or a villager before submitting.", ephemeral=True)
            return

        user_id_int = interaction.user.id
        if user_id_int in _submitting_users:
            await interaction.response.send_message("⏳ An order submission is already in progress. Please wait a moment!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _submitting_users.add(user_id_int)
        try:
            order_cmd = self.cart.to_order_string()
            username = interaction.user.display_name
            user_id = str(user_id_int)

            result = await sysbot.submit_order(
                username=username,
                order_text=order_cmd,
                user_id=user_id,
            )

            if result.get("success") or result.get("order_id"):
                order_id = result.get("order_id", "Unknown")
                pos = result.get("queue_position", 1)
                eta = result.get("estimated_minutes", 2)
                island = result.get("island_name", "Sinta")

                # Clear cart on successful submission
                self.cart.clear()

                embed = discord.Embed(
                    title="✅ Order Successfully Placed!",
                    description=(
                        f"Your pocket order has been submitted to **ChOrder Bot**!\n\n"
                        f"**Order ID:** `{order_id}`\n"
                        f"**Queue Position:** `#{pos}`\n"
                        f"**Estimated Arrival:** `~{eta} min`\n"
                        f"**Island:** 🏝️ `{island}`\n\n"
                        f"**Order Breakdown:**\n```{order_cmd[:900]}```\n"
                        f"You will receive a **direct message** with your **Dodo Code** when the bot is ready. "
                        f"Track this anytime under **My Orders**."
                    ),
                    color=EMBED_COLOR_DEFAULT,
                )
                apply_chopaeng_footer(embed, self.guild_icon)
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=SingleActionCloseView())
            else:
                err = result.get("error") or "Order could not be submitted."
                await interaction.followup.send(f"❌ **Submission Failed:** {err}", ephemeral=True)
        except Exception as exc:
            logger.exception(f"[CartView] Error placing order: {exc}")
            await interaction.followup.send("❌ An unexpected error occurred while submitting your order. Please try again.", ephemeral=True)
        finally:
            _submitting_users.discard(user_id_int)

    async def on_add_item(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddItemModal(self.cart, self.guild_icon))

    async def on_set_villager(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetVillagerModal(self.cart, self.guild_icon))

    async def on_remove_item(self, interaction: discord.Interaction):
        val = interaction.data.get("values", [""])[0]
        idx = int(val.replace("rem_", ""))
        self.cart.remove_item(idx)
        self._build_components()
        embed = build_cart_embed(self.cart, interaction.user, self.guild_icon)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_clear_cart(self, interaction: discord.Interaction):
        if not self.cart.items and not self.cart.villager:
            await interaction.response.send_message("❌ Your cart is already empty.", ephemeral=True)
            return

        confirm_embed = discord.Embed(
            title="🧹 Clear Cart Confirmation",
            description=f"Are you sure you want to clear all **{self.cart.get_pocket_count()}** item(s) from your cart?",
            color=EMBED_COLOR_WARN,
        )
        apply_chopaeng_footer(confirm_embed, self.guild_icon)
        confirm_view = ClearCartConfirmView(self.cart, self.guild_icon)
        await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

    async def on_browse_catalog(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CatalogSearchModal(self.cart, self.guild_icon))


# ── My Orders View (Synced with Website) ─────────────────────────────────────


class MyOrdersView(discord.ui.View):
    """View and track user orders with pagination for past order history (synced with website SQLite DB)."""

    def __init__(self, user_id: str, username: str, orders: List[Dict[str, Any]], page: int = 0, guild_icon: Optional[str] = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.username = username
        self.orders = orders
        self.page = page
        self.page_size = 5
        self.guild_icon = guild_icon
        self.message: Optional[discord.Message] = None
        self._build_components()

    @property
    def past_orders(self) -> List[Dict[str, Any]]:
        active = self.get_active_order()
        return [o for o in self.orders if o != active]

    @property
    def total_pages(self) -> int:
        past = self.past_orders
        if not past:
            return 1
        return max(1, (len(past) + self.page_size - 1) // self.page_size)

    def get_current_page_orders(self) -> List[Dict[str, Any]]:
        past = self.past_orders
        start = self.page * self.page_size
        return past[start : start + self.page_size]

    def _build_components(self):
        self.clear_items()

        # Row 0: Action buttons
        btn_refresh = discord.ui.Button(label="Refresh Status", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

        # If active order exists, allow cancellation
        active_order = self.get_active_order()
        if active_order:
            btn_cancel = discord.ui.Button(
                label=f"Cancel Order ({active_order.get('id', '')[:8]})",
                style=discord.ButtonStyle.danger,
                emoji="❌",
                row=0,
            )
            btn_cancel.callback = self.on_cancel_order
            self.add_item(btn_cancel)

        btn_close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌", row=0)
        btn_close.callback = self.on_close
        self.add_item(btn_close)

        # Row 1: Pagination buttons (if multiple history pages exist)
        if self.total_pages > 1:
            btn_prev = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page <= 0),
                row=1,
            )
            btn_prev.callback = self.on_prev_page
            self.add_item(btn_prev)

            btn_next = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1),
                row=1,
            )
            btn_next.callback = self.on_next_page
            self.add_item(btn_next)

    async def on_prev_page(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self._build_components()
            embed = self.build_orders_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_next_page(self, interaction: discord.Interaction):
        if self.page < self.total_pages - 1:
            self.page += 1
            self._build_components()
            embed = self.build_orders_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_close(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def get_active_order(self) -> Optional[Dict[str, Any]]:
        for o in self.orders:
            st = str(o.get("status") or "").lower()
            if st in ("queued", "preparing", "ready", "active", "next"):
                return o
        return None

    def build_orders_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} My Orders — {self.username}",
            description="Synced with your account and website orders.",
            color=EMBED_COLOR_ORDER,
        )

        active = self.get_active_order()
        if active:
            st = str(active.get("status") or "queued").upper()
            dodo = active.get("dodo_code")
            pos = active.get("queue_position", 1)
            eta = active.get("estimated_minutes", 2)
            island = active.get("island_name", "Sinta")
            cmd = active.get("command", "")

            status_icon = "🟢" if st == "READY" else ("🟡" if st == "PREPARING" else "⏳")

            val = (
                f"**Status:** {status_icon} `{st}`\n"
                f"**Order ID:** `{active.get('id')}`\n"
                f"**Queue Position:** `#{pos}` | **ETA:** `~{eta} min`\n"
                f"**Island:** 🏝️ `{island}`\n"
            )
            if dodo and str(dodo).strip() not in _INVALID_DODO_CODES:
                val += f"\n✈️ **DODO CODE:** ```\n{dodo}\n```\n*Fly to {island} now! Pick up items near airport.*"
            else:
                val += "\n*Dodo code will appear here once ready.*"

            if cmd:
                display_cmd = cmd[:300] + ("..." if len(cmd) > 300 else "")
                val += f"\n**Items:** `{display_cmd}`"

            embed.add_field(name=f"{Config.STAR_PINK} Active Order", value=val, inline=False)
        else:
            embed.add_field(
                name=f"{Config.STAR_PINK} No Active Orders",
                value="You do not have any pending orders in the queue.",
                inline=False,
            )

        # Past orders history with pagination
        past = self.get_current_page_orders()
        if past:
            lines = []
            for o in past:
                st = str(o.get("status") or "").upper()
                st_badge = "✅" if st == "COMPLETED" else ("❌" if st in ("CANCELLED", "ERROR") else "📦")
                ts = o.get("created_at")
                time_str = f"<t:{ts}:R>" if ts else ""
                cmd_snip = o.get("command", "")
                if cmd_snip:
                    cmd_snip = f" — *`{cmd_snip[:30] + ('...' if len(cmd_snip) > 30 else '')}`*"
                lines.append(f"{st_badge} `{o.get('id')[:10]}` — `{st}` {time_str}{cmd_snip}")

            history_title = f"{Config.STAR_PINK} Order History (Page {self.page + 1}/{self.total_pages})" if self.total_pages > 1 else f"{Config.STAR_PINK} Recent Order History"
            add_chunked_fields(embed, history_title, lines, max_chars=950)

        apply_chopaeng_footer(embed, self.guild_icon)
        return embed

    async def on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.orders = await sysbot.get_user_order_history(self.user_id, limit=50)
            active = self.get_active_order()
            if active and active.get("id"):
                fresh = await sysbot.get_order_status(active["id"])
                if fresh.get("status"):
                    active.update(fresh)

            if self.page >= self.total_pages:
                self.page = max(0, self.total_pages - 1)

            self._build_components()
            embed = self.build_orders_embed()
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
        except Exception as exc:
            logger.exception(f"[MyOrdersView] Error refreshing status: {exc}")
            await interaction.followup.send("❌ Error fetching latest order status.", ephemeral=True)

    async def on_cancel_order(self, interaction: discord.Interaction):
        active = self.get_active_order()
        if not active:
            await interaction.response.send_message("❌ No active order found to cancel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            res = await sysbot.cancel_order(active["id"])
            self.orders = await sysbot.get_user_order_history(self.user_id, limit=50)
            if self.page >= self.total_pages:
                self.page = max(0, self.total_pages - 1)
            self._build_components()
            embed = self.build_orders_embed()
            await interaction.followup.send("✅ Order has been cancelled.", ephemeral=True)
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
        except Exception as exc:
            logger.exception(f"[MyOrdersView] Error cancelling order: {exc}")
            await interaction.followup.send("❌ Could not cancel order. It may have already finished or expired.", ephemeral=True)


# ── Live Queue View ──────────────────────────────────────────────────────────


class LiveQueueView(discord.ui.View):
    """View current ChOrder Bot orders queue."""

    def __init__(self, queue_data: dict, bot_status: dict, guild_icon: Optional[str] = None):
        super().__init__(timeout=180)
        self.queue_data = queue_data
        self.bot_status = bot_status
        self.guild_icon = guild_icon
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self):
        self.clear_items()
        btn_refresh = discord.ui.Button(label="Refresh Queue", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

        btn_close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌", row=0)
        btn_close.callback = self.on_close
        self.add_item(btn_close)

    async def on_close(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def build_queue_embed(self) -> discord.Embed:
        island = self.bot_status.get("island_name", "Sinta")
        is_online = self.bot_status.get("is_running", True)
        orders = self.queue_data.get("orders") or self.queue_data.get("queue") or []

        embed = discord.Embed(
            title=f"{Config.EMOJI_SEARCH} Live Order Queue — {island}",
            description=(
                f"**Island Status:** {'🟢 Online' if is_online else '🔴 Offline'}\n"
                f"**Orders in Queue:** `{len(orders)}`\n"
            ),
            color=EMBED_COLOR_ORDER,
        )

        if orders:
            lines = []
            for idx, o in enumerate(orders[:15], 1):
                user = o.get("username") or o.get("user") or "Anonymous"
                st = str(o.get("status") or "queued").upper()
                eta = o.get("estimated_minutes") or o.get("eta") or 2
                lines.append(f"`#{idx:02d}` **{user}** — `{st}` (~{eta}m)")

            add_chunked_fields(embed, f"{Config.STAR_PINK} Current Queue", lines, max_chars=950)
            if len(orders) > 15:
                embed.add_field(name=f"{Config.STAR_PINK} Remaining", value=f"*+{len(orders)-15} more in line...*", inline=False)
        else:
            embed.add_field(
                name=f"{Config.STAR_PINK} Queue Empty",
                value="No orders in queue! Place an order to be served immediately.",
                inline=False,
            )

        apply_chopaeng_footer(embed, self.guild_icon)
        return embed

    async def on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.queue_data = await sysbot.get_queue()
            self.bot_status = await sysbot.get_bot_status()
            self._build_components()
            embed = self.build_queue_embed()
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
        except Exception as exc:
            logger.exception(f"[LiveQueueView] Error refreshing queue: {exc}")
            await interaction.followup.send("❌ Error fetching queue data.", ephemeral=True)


# ── Main Order Panel View (Channel Deployable) ───────────────────────────────


class OrderPanelView(discord.ui.View):
    """Persistent interactive panel deployed in channels."""

    def __init__(self):
        super().__init__(timeout=None)  # Persistent view
        self.add_item(
            discord.ui.Button(
                label="Open Web Builder",
                style=discord.ButtonStyle.link,
                url="https://www.chopaeng.com/order",
                emoji="🌐",
                row=2,
            )
        )

    @discord.ui.button(
        label="Add to Cart / My Cart",
        style=discord.ButtonStyle.primary,
        emoji="🛒",
        custom_id="panel_btn_cart",
        row=0,
    )
    async def btn_cart(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_icon = resolve_guild_icon(interaction)
        cart = cart_manager.get_cart(interaction.user.id)
        embed = build_cart_embed(cart, interaction.user, guild_icon)
        view = CartView(cart, guild_icon)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Search Catalog",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="panel_btn_search",
        row=0,
    )
    async def btn_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_icon = resolve_guild_icon(interaction)
        cart = cart_manager.get_cart(interaction.user.id)
        await interaction.response.send_modal(CatalogSearchModal(cart, guild_icon))

    @discord.ui.button(
        label="Presets / Bundles",
        style=discord.ButtonStyle.secondary,
        emoji="📦",
        custom_id="panel_btn_presets",
        row=0,
    )
    async def btn_presets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_icon = resolve_guild_icon(interaction)
            cart = cart_manager.get_cart(interaction.user.id)
            bundles = await sysbot.get_bundles()
            view = BundlesView(cart, bundles, guild_icon)
            embed = view.build_bundle_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.exception(f"[OrderPanelView] Error loading bundles: {exc}")
            await interaction.followup.send("❌ Failed to load preset bundles. Please try again later.", ephemeral=True)

    @discord.ui.button(
        label="My Orders",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="panel_btn_orders",
        row=1,
    )
    async def btn_orders(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_icon = resolve_guild_icon(interaction)
            user_id = str(interaction.user.id)
            orders = await sysbot.get_user_order_history(user_id, limit=50)
            view = MyOrdersView(user_id, interaction.user.display_name, orders, guild_icon=guild_icon)
            embed = view.build_orders_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.exception(f"[OrderPanelView] Error loading orders: {exc}")
            await interaction.followup.send("❌ Failed to fetch your orders.", ephemeral=True)

    @discord.ui.button(
        label="Live Queue",
        style=discord.ButtonStyle.secondary,
        emoji="⏱️",
        custom_id="panel_btn_queue",
        row=1,
    )
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_icon = resolve_guild_icon(interaction)
            q_data = await sysbot.get_queue()
            status_data = await sysbot.get_bot_status()
            view = LiveQueueView(q_data, status_data, guild_icon)
            embed = view.build_queue_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as exc:
            logger.exception(f"[OrderPanelView] Error loading queue: {exc}")
            await interaction.followup.send("❌ Failed to fetch current queue status.", ephemeral=True)

    @discord.ui.button(
        label="Quick Order",
        style=discord.ButtonStyle.success,
        emoji="⚡",
        custom_id="panel_btn_quick_order",
        row=1,
    )
    async def btn_quick_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuickOrderModal())


# ============================================================================
# DISCORD COG & COMMAND HANDLER
# ============================================================================


class ChorderCog(commands.Cog, name="Chorder"):
    """Cog providing the interactive ACNH ChOrder Bot Order Panel and DM Notification Engine."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.stale_cart_task.start()
        self.order_dm_notifier_task.start()

    def cog_unload(self):
        self.stale_cart_task.cancel()
        self.order_dm_notifier_task.cancel()

    @tasks.loop(minutes=30)
    async def stale_cart_task(self):
        """Periodically clean up inactive carts to prevent memory leaks."""
        try:
            cart_manager.cleanup_stale_carts(max_age_seconds=7200)
        except Exception as exc:
            logger.warning(f"[ChorderCog] Stale cart cleanup encountered: {exc}")

    @stale_cart_task.before_loop
    async def before_stale_cart_task(self):
        while not self.bot.is_ready():
            await asyncio.sleep(0.5)

    @tasks.loop(seconds=12)
    async def order_dm_notifier_task(self):
        """
        Background task to notify users via Discord DM when:
        1. An order is ready with a Dodo code ('ready')
        2. An order is successfully finished ('completed')
        3. An order is cancelled or encounters an error ('cancelled' / 'error')
        Uses the persistent SQLite 'order_notifications' table to guarantee
        that each notification event is only dispatched ONCE across bot restarts.
        """
        try:
            with connect_db() as conn:
                # 1. Ready orders with valid Dodo Code
                ready_rows = conn.execute(
                    """
                    SELECT id, user_id, username, command, island_name, dodo_code, updated_at
                    FROM order_bot_queue
                    WHERE status IN ('ready', 'active')
                      AND dodo_code IS NOT NULL
                      AND dodo_code NOT IN ('', '00000', '-----', 'null', 'None')
                      AND id NOT IN (
                          SELECT order_id FROM order_notifications WHERE notification_type = 'ready'
                      )
                    ORDER BY updated_at DESC
                    LIMIT 15
                    """
                ).fetchall()

                # 2. Completed orders (that were previously ready)
                completed_rows = conn.execute(
                    """
                    SELECT id, user_id, username, command, island_name, dodo_code, updated_at
                    FROM order_bot_queue
                    WHERE status = 'completed'
                      AND id IN (
                          SELECT order_id FROM order_notifications WHERE notification_type = 'ready'
                      )
                      AND id NOT IN (
                          SELECT order_id FROM order_notifications WHERE notification_type = 'completed'
                      )
                    ORDER BY updated_at DESC
                    LIMIT 15
                    """
                ).fetchall()

                # 3. Cancelled/Error orders
                error_rows = conn.execute(
                    """
                    SELECT id, user_id, username, command, island_name, message, updated_at
                    FROM order_bot_queue
                    WHERE status IN ('cancelled', 'error')
                      AND id NOT IN (
                          SELECT order_id FROM order_notifications WHERE notification_type IN ('cancelled', 'error', 'completed')
                      )
                    ORDER BY updated_at DESC
                    LIMIT 15
                    """
                ).fetchall()

            for r in ready_rows:
                await self._dispatch_dm(r["id"], r["user_id"], "ready", r["island_name"], r["dodo_code"], r["command"])

            for r in completed_rows:
                await self._dispatch_dm(r["id"], r["user_id"], "completed", r["island_name"], r["dodo_code"], r["command"])

            for r in error_rows:
                await self._dispatch_dm(r["id"], r["user_id"], "cancelled", r["island_name"], "", r.get("message") or "Order cancelled.")

        except Exception as exc:
            logger.debug(f"[ChorderNotifier] Order DM notifier loop encountered: {exc}")

    async def _dispatch_dm(
        self,
        order_id: str,
        user_id_str: str,
        notif_type: str,
        island: str = "Sinta",
        dodo_code: str = "",
        extra_text: str = "",
    ):
        user_id_str = str(user_id_str or "").strip()
        if not user_id_str or not user_id_str.isdigit():
            self._record_notification(order_id, user_id_str, notif_type, dodo_code)
            return

        uid_int = int(user_id_str)
        user = self.bot.get_user(uid_int)
        if not user:
            try:
                user = await self.bot.fetch_user(uid_int)
            except Exception:
                user = None

        if user:
            if notif_type == "ready":
                embed = discord.Embed(
                    title="✈️ Pack Your Bags! Your ACNH Order is Ready!",
                    description=(
                        f"Hello {user.mention}! Your order has been prepared and is waiting on **{island}**.\n\n"
                        f"🔑 **DODO CODE:**\n```yaml\n{dodo_code}\n```\n"
                        f"**Island:** 🏝️ `{island}`\n"
                        f"**Order ID:** `{order_id}`\n\n"
                        f"**Pickup Instructions:**\n"
                        f"1. Head to **Dodo Airlines** on your Nintendo Switch.\n"
                        f"2. Select **'I want to fly!'** ➔ **'Via online play'** ➔ **'Via Dodo Code™'**.\n"
                        f"3. Enter the code `{dodo_code}` above.\n"
                        f"4. Pick up your items near the airport and return home safely!\n"
                    ),
                    color=EMBED_COLOR_DEFAULT,
                )
                if extra_text:
                    display_cmd = extra_text if len(extra_text) <= 250 else f"{extra_text[:250]}..."
                    embed.add_field(name="📦 Order Contents", value=f"`{display_cmd}`", inline=False)
                embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
                apply_chopaeng_footer(embed)

            elif notif_type == "completed":
                embed = discord.Embed(
                    title="🎉 Order Complete! Thanks for Visiting!",
                    description=(
                        f"Hello {user.mention}! Your order `{order_id}` on **{island}** has been marked **Completed**.\n\n"
                        f"Enjoy your new items! Need more supplies? Use `/orderpanel` or visit [chopaeng.com](https://console.chopaeng.com/orderbot) anytime."
                    ),
                    color=EMBED_COLOR_DEFAULT,
                )
                embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
                apply_chopaeng_footer(embed)

            else:  # cancelled / error
                embed = discord.Embed(
                    title="⚠️ Order Cancelled / Closed",
                    description=(
                        f"Hello {user.mention}! Your order `{order_id}` was cancelled or closed.\n\n"
                        f"**Reason / Message:** `{extra_text or 'Order cancelled or expired.'}`\n\n"
                    ),
                    color=EMBED_COLOR_WARN,
                )
                apply_chopaeng_footer(embed)

            try:
                await user.send(embed=embed)
                logger.info(f"[ChorderNotifier] Sent {notif_type} DM to {user} ({uid_int}) for order {order_id}.")
            except discord.Forbidden:
                logger.warning(f"[ChorderNotifier] Could not DM user {uid_int} (DMs closed/disabled).")
            except Exception as exc:
                logger.warning(f"[ChorderNotifier] Failed to DM user {uid_int}: {exc}")

        # Mark as notified in persistent DB to prevent resending on restarts
        self._record_notification(order_id, user_id_str, notif_type, dodo_code)

    def _record_notification(self, order_id: str, user_id: str, notification_type: str, dodo_code: str):
        """Persist notification record so duplicate DMs are never sent across bot restarts."""
        try:
            with connect_db() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO order_notifications (order_id, user_id, notification_type, dodo_code, sent_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_id, user_id, notification_type, dodo_code, int(time.time()))
                )
        except Exception as exc:
            logger.warning(f"[ChorderNotifier] Failed to record notification in DB: {exc}")

    @order_dm_notifier_task.before_loop
    async def before_order_dm_notifier_task(self):
        while not self.bot.is_ready():
            await asyncio.sleep(0.5)

    # ── Slash Command: /catalog ─────────────────────────────────────────────

    async def catalog_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for the /catalog command backed by ACNHCatalog."""
        if not current or len(current) < 2:
            return []
        try:
            item_hits = catalog.search_items(current, limit=18)
            villager_hits = catalog.search_villagers(current, limit=7)

            choices: list[app_commands.Choice[str]] = []
            seen: set[str] = set()

            for item in item_hits:
                label = item.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"📦 {label}"[:100], value=label))

            for v in villager_hits:
                label = v.display_name[:100]
                if label not in seen:
                    seen.add(label)
                    choices.append(app_commands.Choice(name=f"🐱 {label}"[:100], value=label))

            return choices[:25]
        except Exception as exc:
            logger.warning(f"[ChorderCog] catalog_autocomplete error: {exc}")
            return []

    @app_commands.command(
        name="catalog",
        description="Search the ACNH catalog — browse items, add to cart, or set a villager",
    )
    @app_commands.describe(query="Item or villager name to search for")
    @app_commands.autocomplete(query=catalog_autocomplete)
    async def slash_catalog(self, interaction: discord.Interaction, query: str = ""):
        """Interactive ACNH catalog search with pagination and cart integration."""
        await interaction.response.defer(ephemeral=True)

        try:
            guild_icon = resolve_guild_icon(interaction, self.bot)
            cart = cart_manager.get_cart(interaction.user.id)

            search_query = query.strip()
            items = catalog.search_items(search_query, limit=100) if search_query else []
            villagers = catalog.search_villagers(search_query, limit=10) if search_query else []

            if not search_query:
                # No query given — prompt user to provide a keyword
                await interaction.followup.send(
                    "Use the **Search Catalog** button on the Order Panel, or type a keyword with `/catalog <item>`.",
                    ephemeral=True,
                )
                return

            view = CatalogSearchView(
                cart=cart,
                query=search_query,
                items=items,
                villagers=villagers,
                page=0,
                guild_icon=guild_icon,
            )
            embed = view.build_search_embed()
            msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            view.message = msg
            logger.info(
                f"[ChorderCog] /catalog '{search_query}' by {interaction.user} → "
                f"{len(items)} items, {len(villagers)} villagers"
            )
        except Exception as exc:
            logger.exception(f"[ChorderCog] /catalog error: {exc}")
            await interaction.followup.send(
                "❌ Failed to search the catalog. Please try again later.", ephemeral=True
            )

    # ── Slash Command: /orderpanel ──────────────────────────────────────────

    @app_commands.command(
        name="orderpanel",
        description="Deploy the interactive ACNH ChOrder Bot Order Panel to a channel",
    )
    @app_commands.describe(channel="The channel to deploy the order panel into (defaults to current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_orderpanel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        perms = interaction.user.guild_permissions if hasattr(interaction.user, "guild_permissions") else None
        if perms and not (perms.manage_channels or perms.administrator):
            await interaction.response.send_message(
                "❌ You need 'Manage Channels' or Administrator permission to deploy the order panel.",
                ephemeral=True,
            )
            return

        target_channel = channel or interaction.channel
        if not target_channel:
            await interaction.response.send_message("❌ Target channel not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            bot_status = await sysbot.get_bot_status()
            island_name = bot_status.get("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
            is_online = bot_status.get("is_running", True)
            guild_icon = resolve_guild_icon(target_channel or interaction, self.bot)

            embed = build_panel_embed(island_name, is_online, guild_icon=guild_icon)
            view = OrderPanelView()

            await target_channel.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Order panel deployed to {target_channel.mention}!", ephemeral=True
            )
        except Exception as exc:
            logger.exception(f"[slash_orderpanel] Deployment error: {exc}")
            await interaction.followup.send("❌ Failed to deploy order panel. Check bot permissions in that channel.", ephemeral=True)


# ============================================================================
# STANDALONE BOT RUNNER
# ============================================================================


class ChorderBot(commands.Bot):
    """Standalone Discord Bot instance for Chorder."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        logger.info("[Chorder] Loading Chorder Cog...")
        await self.add_cog(ChorderCog(self))
        self.add_view(OrderPanelView())  # Register persistent view

        # Sync slash commands
        if Config.GUILD_ID:
            guild_obj = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info(f"[Chorder] Slash commands synced to Guild: {Config.GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("[Chorder] Slash commands synced globally.")

    async def on_ready(self):
        logger.info(f"[Chorder] Logged in as: {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="ACNH Orders | /orderpanel 📦",
            )
        )


def run_standalone():
    """Entry point for standalone execution."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    token = Config.DISCORD_TOKEN or os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("[Chorder] DISCORD_TOKEN is not set in .env! Cannot start bot.")
        sys.exit(1)

    logger.info("[Chorder] Starting standalone Chorder Bot...")
    bot = ChorderBot()
    try:
        bot.run(token)
    except KeyboardInterrupt:
        logger.info("[Chorder] Bot stopped by user.")
    except Exception as exc:
        logger.error(f"[Chorder] Bot encountered a fatal error: {exc}", exc_info=True)


if __name__ == "__main__":
    run_standalone()
