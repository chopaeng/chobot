"""SQLAlchemy models for ChoBot's application database."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Integer, String, Text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CommandClaim(Base):
    __tablename__ = "command_claims"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claimed_at: Mapped[float] = mapped_column(Float, nullable=False)


class IslandSubscription(Base):
    __tablename__ = "island_subscriptions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    island_clean: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True, default="sub")
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column("key", String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Island(Base):
    __tablename__ = "islands"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal")
    cat: Mapped[str] = mapped_column(String(64), nullable=False, default="public")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seasonal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="OFFLINE")
    visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dodo_code: Mapped[str | None] = mapped_column(String(32))
    map_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(String(64))
    required_roles: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    channel_id: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_visible: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class IslandBotStatus(Base):
    __tablename__ = "island_bot_status"

    island_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_online: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandMetadata(Base):
    __tablename__ = "island_metadata"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="public")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandVisit(Base):
    __tablename__ = "island_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ign: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_island: Mapped[str] = mapped_column(String(255), nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    authorized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    island_type: Mapped[str] = mapped_column(String(64), nullable=False, default="sub", index=True)
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_island_visits_user_guild_ts", "user_id", "guild_id", "timestamp"),
        Index("ix_island_visits_ign_ts", "ign", "timestamp"),
    )


class Warning(Base):
    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    mod_id: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[int | None] = mapped_column(BigInteger, index=True)
    visit_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, default="WARN", index=True)

    __table_args__ = (
        Index("ix_warnings_user_guild_ts", "user_id", "guild_id", "timestamp"),
    )


class DodoRevealMessage(Base):
    __tablename__ = "dodo_reveal_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    island_clean: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(64))
    message_url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class MemberIdentityEvent(Base):
    __tablename__ = "member_identity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    old_display_name: Mapped[str | None] = mapped_column(String(255))
    new_display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_member_identity_events_user_guild_ts", "user_id", "guild_id", "created_at"),
    )


class DashboardAuditEvent(Base):
    __tablename__ = "dashboard_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), index=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_dashboard_audit_action_ts", "action", "created_at"),
    )


class CommandSearchEvent(Base):
    __tablename__ = "command_search_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    normalized_query: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), index=True)
    found: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_command_search_command_ts", "command", "created_at"),
    )


class SearchAlias(Base):
    __tablename__ = "search_aliases"

    alias: Mapped[str] = mapped_column(String(255), primary_key=True)
    target: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="item", primary_key=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class DodoQueueEntry(Base):
    __tablename__ = "dodo_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    island_clean: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="waiting", index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_dodo_queue_island_status_ts", "island_clean", "status", "created_at"),
    )


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CommunityLoadout(Base):
    __tablename__ = "community_loadouts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    short_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(String(255), nullable=False, default="[]")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="General")
    order_items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    drop_items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="Community")
    upvotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_official: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_community_loadouts_upvotes", "upvotes"),
        Index("ix_community_loadouts_category", "category"),
    )




class CommunityLoadoutUpvote(Base):
    __tablename__ = "community_loadout_upvotes"

    loadout_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_loadout_upvotes_user", "user_id"),
    )


class UserSavedCharacter(Base):
    __tablename__ = "user_saved_characters"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ign: Mapped[str] = mapped_column(String(255), nullable=False)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    icon: Mapped[str | None] = mapped_column(String(64))
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_saved_characters_user_id", "user_id"),
    )


class UserFavoriteIsland(Base):
    __tablename__ = "user_favorite_islands"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    island_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_fav_islands_user_id", "user_id"),
    )


class UserPublicPassport(Base):
    __tablename__ = "user_public_passports"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    is_public: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    show_character_and_island: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pronouns: Mapped[str | None] = mapped_column(String(64))
    birth_day: Mapped[str | None] = mapped_column(String(16))
    birth_month: Mapped[str | None] = mapped_column(String(32))
    native_fruit: Mapped[str | None] = mapped_column(String(32))
    favourite_colour: Mapped[str | None] = mapped_column(String(32))
    favourite_song: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str | None] = mapped_column(String(64))
    personality: Mapped[str | None] = mapped_column(String(64))
    hobbies: Mapped[str | None] = mapped_column(String(255))
    favourite_shows_films: Mapped[str | None] = mapped_column(String(255))
    about_you: Mapped[str | None] = mapped_column(Text)
    favourite_villagers: Mapped[str | None] = mapped_column(Text)
    primary_ign: Mapped[str | None] = mapped_column(String(64))
    primary_island: Mapped[str | None] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_public_passports_username", "username"),
    )


class OrderBotQueue(Base):
    __tablename__ = "order_bot_queue"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    command: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(String(64), nullable=False, default="order")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="queued", index=True)
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    dodo_code: Mapped[str | None] = mapped_column(String(32))
    island_name: Mapped[str | None] = mapped_column(String(255), default="Sinta")
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_order_bot_queue_user_status", "user_id", "status"),
    )


class OrderNotification(Base):
    __tablename__ = "order_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False, default="ready")
    dodo_code: Mapped[str | None] = mapped_column(String(32))
    sent_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_order_notifications_order_type", "order_id", "notification_type", unique=True),
    )


class UserCustomPreset(Base):
    __tablename__ = "user_custom_presets"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[str | None] = mapped_column(Text)
    order_items: Mapped[str | None] = mapped_column(Text)
    drop_items: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class SharedPocket(Base):
    __tablename__ = "shared_pockets"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    villager: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, index=True)


class PocketBundle(Base):
    __tablename__ = "pocket_bundles"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="General")
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    villager: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(255), default="Admin")
    is_official: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_pocket_bundles_cat", "category", "is_official"),
    )


class IncidentWorkflow(Base):
    __tablename__ = "incident_workflow"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="open", index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    resolved_at: Mapped[int | None] = mapped_column(BigInteger)


class WebsiteLoginEvent(Base):
    __tablename__ = "website_login_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    avatar: Mapped[str | None] = mapped_column(Text)
    roles: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    highest_role: Mapped[str | None] = mapped_column(String(255))
    highest_role_color: Mapped[int | None] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    logged_in_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        Index("ix_website_login_user_ts", "user_id", "logged_in_at"),
    )


class UserCollectionItem(Base):
    __tablename__ = "user_collection_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_collection_uid_item", "user_id", "item_id", unique=True),
    )


class UserWishlistItem(Base):
    __tablename__ = "user_wishlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_wishlist_uid_item", "user_id", "item_id", unique=True),
    )


class UserPocketState(Base):
    __tablename__ = "user_pocket_states"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    drop_items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    villager: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class UserCaughtCritter(Base):
    __tablename__ = "user_caught_critters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    critter_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    hemisphere: Mapped[str] = mapped_column(String(16), nullable=False, default="north")
    caught_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_user_critters_uid_name_hemi", "user_id", "critter_name", "hemisphere", unique=True),
    )


class UserInAppNotification(Base):
    __tablename__ = "user_in_app_notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    is_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_user_notifications_uid_ts", "user_id", "timestamp"),
    )
