"""
24/7 Discord DM Bot for SEC EDGAR (Form D) & SEDAR+ (Form 45-106F1) Intelligence.
Locked exclusively to OWNER_DISCORD_ID.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import discord

# When compiled with --noconsole on Windows, sys.stdout/stderr may be None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from discord import app_commands
from discord.ext import tasks

from auditor import AuditSession
from config import settings
from filters import FilterEngine
from integrations import EnrichmentPipeline
from models import AuditRun, EnrichedExecutive, NormalizedFiling
from sources import SecEdgarSource, SedarPlusSource
from storage import StateStore


def is_authorized_owner(user_id: int) -> bool:
    """Hard security gate: checks if user_id is in configured authorized accounts."""
    return bool(settings.AUTHORIZED_USER_IDS) and user_id in settings.AUTHORIZED_USER_IDS


def _clickable_url(url: str) -> str:
    """Ensures a URL has an http/https scheme and is returned as a bare URL so Discord auto-links it."""
    u = url.strip()
    if u and not u.startswith(("http://", "https://")):
        return f"https://{u}"
    return u


def build_filing_embed(filing: NormalizedFiling, run: AuditRun) -> discord.Embed:
    """Formats the 7 required company metadata fields into a clean Discord DM Embed."""
    source_badge = "🇺🇸 SEC EDGAR (Form D)" if filing.source == "SEC_EDGAR" else "🇨🇦 SEDAR+ (45-106F1)"
    clean_site = _clickable_url(filing.company_website) if filing.company_website else ""
    website_display = (
        f"{clean_site} *(TypeSafe QC: {filing.website_confidence:.0%})*"
        if clean_site and filing.website_confidence is not None
        else (clean_site or "Not found / did not pass TypeSafe QC")
    )

    embed = discord.Embed(
        title=f"🚀 New Qualifying Filing: {filing.company_name}",
        url=filing.filing_url or None,
        description=f"**Source:** {source_badge} | **Filing ID:** `{filing.filing_id}`\n"
        f"**Filing Type(s):** `{', '.join(filing.filing_types)}`",
        color=discord.Color.from_rgb(46, 204, 113),
    )
    # Exact 7 required metadata fields
    embed.add_field(
        name="1. Number of Executive Directors",
        value=str(filing.num_executive_directors),
        inline=True,
    )
    embed.add_field(
        name="2. Name of the Executive Director(s)",
        value=filing.executive_names_formatted[:1024],
        inline=False,
    )
    embed.add_field(
        name="3. Name of the Company",
        value=filing.company_name,
        inline=True,
    )
    embed.add_field(
        name="4. Industry",
        value=filing.industry,
        inline=True,
    )
    embed.add_field(
        name="5. Total Amount Invested (All Rounds)",
        value=f"${filing.total_amount_all_rounds_usd:,.2f} USD",
        inline=True,
    )
    embed.add_field(
        name="6. Amount Invested (This Round)",
        value=f"${filing.amount_this_round_usd:,.2f} USD",
        inline=True,
    )
    embed.add_field(
        name="7. Company Website Link",
        value=website_display,
        inline=False,
    )
    embed.set_footer(
        text=(
            f"Audit ID: {run.run_id} | Steps: {len(run.steps)} | "
            f"Tokens: {run.total_tokens:,} | Run Cost: ${run.total_cost_usd:.5f}"
        )
    )
    return embed


def build_executive_embed(exec_data: EnrichedExecutive) -> discord.Embed:
    """Formats the 5 required deep-dive fields for a chosen Executive Director."""
    embed = discord.Embed(
        title=f"🕵️ Executive Deep-Dive: {exec_data.full_name}",
        description=f"**Company:** {exec_data.company_name}",
        color=discord.Color.from_rgb(52, 152, 219),
    )
    clean_li = _clickable_url(exec_data.linkedin_url) if exec_data.linkedin_url else ""
    li_val = (
        f"{clean_li} *(TypeSafe QC: {exec_data.linkedin_confidence:.0%})*"
        if clean_li and exec_data.linkedin_confidence is not None
        else (clean_li or "None verified")
    )
    embed.add_field(name="1. LinkedIn Profile", value=li_val, inline=False)
    embed.add_field(
        name="2. Social Media Handles",
        value="\n".join(exec_data.social_handles) if exec_data.social_handles else "None verified",
        inline=False,
    )
    embed.add_field(
        name="3. Blogs",
        value="\n".join(exec_data.blogs) if exec_data.blogs else "None found",
        inline=False,
    )
    embed.add_field(
        name="4. Personal Websites",
        value="\n".join(exec_data.personal_websites) if exec_data.personal_websites else "None found",
        inline=False,
    )
    email_val = (
        f"`{exec_data.valid_email}` — **{exec_data.email_verification_status}**"
        if exec_data.valid_email
        else f"None ({exec_data.email_verification_status})"
    )
    embed.add_field(name="5. Valid Email Address", value=email_val, inline=False)
    return embed


class AuditOnlyView(discord.ui.View):
    """Attached to deep-dive results so the user can audit the Apify/Exa/TypeSafe cost."""

    def __init__(self, store: StateStore, audit_run_id: str) -> None:
        super().__init__(timeout=None)
        self.store = store
        self.audit_run_id = audit_run_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_authorized_owner(interaction.user.id)

    @discord.ui.button(
        label="📊 Audit Deep-Dive Steps & Cost", style=discord.ButtonStyle.secondary
    )
    async def show_audit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        run = self.store.get_audit(self.audit_run_id)
        if not run:
            await interaction.response.send_message("Audit log not found.", ephemeral=True)
            return
        session = AuditSession(run.run_type, run.target_label)
        session.run = run
        await interaction.response.send_message(session.format_discord_report()[:1990])


class ExecutiveSelect(discord.ui.Select):
    """Multi-select dropdown to request deep-dive info on specific Executive Directors."""

    def __init__(
        self,
        filing: NormalizedFiling,
        pipeline: EnrichmentPipeline,
        store: StateStore,
    ) -> None:
        self.filing = filing
        self.pipeline = pipeline
        self.store = store
        options = [
            discord.SelectOption(
                label=e.full_name[:100],
                description=("/".join(e.roles))[:100],
                value=str(idx),
            )
            for idx, e in enumerate(filing.executives[:25])
        ]
        super().__init__(
            placeholder="🕵️ Request more info on Executive Director(s)...",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_authorized_owner(interaction.user.id):
            return
        await interaction.response.defer(thinking=True)
        chosen = [self.filing.executives[int(v)] for v in self.values]
        names = ", ".join(c.full_name for c in chosen)
        audit = AuditSession(
            run_type="executive_deep_dive",
            target_label=f"Deep-Dive: {names} ({self.filing.company_name})",
        )
        embeds: list[discord.Embed] = []
        for exec_dir in chosen:
            enriched = await self.pipeline.deep_dive_executive(
                exec_info=exec_dir, filing=self.filing, audit=audit
            )
            embeds.append(build_executive_embed(enriched))

        run = audit.finish("COMPLETED")
        self.store.save_audit(run)
        await interaction.followup.send(
            content=f"✅ Completed deep-dive for **{names}** (`${run.total_cost_usd:.5f}` total cost):",
            embeds=embeds[:10],
            view=AuditOnlyView(self.store, run.run_id),
        )


class FilingAlertView(discord.ui.View):
    """Interactive buttons and dropdown attached to every Form D / 45-106F1 DM alert."""

    def __init__(
        self,
        filing: NormalizedFiling,
        audit_run_id: str,
        store: StateStore,
        filter_engine: FilterEngine,
        pipeline: EnrichmentPipeline,
    ) -> None:
        super().__init__(timeout=None)
        self.filing = filing
        self.audit_run_id = audit_run_id
        self.store = store
        self.filter_engine = filter_engine
        if filing.executives:
            self.add_item(ExecutiveSelect(filing, pipeline, store))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_authorized_owner(interaction.user.id)

    @discord.ui.button(
        label="📊 Audit Steps & Cost", style=discord.ButtonStyle.primary
    )
    async def audit_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        run = self.store.get_audit(self.audit_run_id)
        if not run:
            await interaction.response.send_message("Audit log not found.")
            return
        session = AuditSession(run.run_type, run.target_label)
        session.run = run
        await interaction.response.send_message(session.format_discord_report()[:1990])

    @discord.ui.button(
        label="🚫 Exclude This Industry", style=discord.ButtonStyle.danger
    )
    async def exclude_industry_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        audit = AuditSession("rule_parse", f"Exclude industry {self.filing.industry}")
        rule = await self.filter_engine.parse_and_add_user_rule(
            f"exclude industry: {self.filing.industry}", audit
        )
        self.store.save_audit(audit.finish("ADDED_RULE"))
        await interaction.response.send_message(
            f"🛑 Added Rule #{rule.id}: `{rule.raw_text}`"
        )


class FormDScoutBot(discord.Client):
    """24/7 Discord DM bot orchestrating polling, filtering, enrichment, and auditing."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = StateStore()
        self.filter_engine = FilterEngine(self.store)
        self.pipeline = EnrichmentPipeline()
        self.edgar_source = SecEdgarSource(self.store)
        self.sedar_source = SedarPlusSource(self.store)
        self._register_slash_commands()

    async def setup_hook(self) -> None:
        interval = float(
            self.store.get_setting(
                "poll_interval_minutes", str(settings.DEFAULT_POLL_INTERVAL_MINUTES)
            )
        )
        self.poll_filings_loop.change_interval(minutes=max(1.0, interval))
        self.poll_filings_loop.start()
        await self.tree.sync()

    async def on_ready(self) -> None:
        owners_str = ", ".join(str(uid) for uid in settings.AUTHORIZED_USER_IDS)
        print(
            f"[FormDScoutBot] Logged in as {self.user} (Authorized Users: {owners_str})",
            flush=True,
        )
        dms = await self.fetch_all_authorized_dms()
        interval = self.store.get_setting(
            "poll_interval_minutes", str(settings.DEFAULT_POLL_INTERVAL_MINUTES)
        )
        for dm in dms:
            try:
                await dm.send(
                    f"🟢 **Form D & SEDAR+ Scout Bot is online!**\n"
                    f"• Polling every **{interval} minute(s)**.\n"
                    f"• Commands: `start`, `stop`, `poll now`, `rules`, `interval <mins>`, "
                    f"`exclude filing type: debt`, `more info on <Executive Name>`, `audit`."
                )
            except Exception as exc:
                print(f"[Warning] Failed to send on_ready greeting to DM: {exc}", flush=True)

    async def fetch_all_authorized_dms(self) -> list[discord.DMChannel]:
        channels: list[discord.DMChannel] = []
        for uid in settings.AUTHORIZED_USER_IDS:
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
                if user:
                    dm = user.dm_channel or await user.create_dm()
                    if dm:
                        channels.append(dm)
            except Exception as exc:
                print(f"[Warning] Could not open DM with {uid}: {exc}", flush=True)
        return channels

    @tasks.loop(minutes=15.0)
    async def poll_filings_loop(self) -> None:
        await self.wait_until_ready()
        await self.run_single_poll_cycle(force_sedar=False)

    async def run_single_poll_cycle(self, force_sedar: bool = False) -> int:
        """Polls SEC EDGAR and SEDAR+, applies Stage 1 & 2 filters, enriches, and DMs all authorized users."""
        dms = await self.fetch_all_authorized_dms()
        if not dms:
            return 0

        candidates: list[tuple[NormalizedFiling, AuditSession]] = []
        try:
            candidates.extend(await self.edgar_source.fetch_latest_candidates(max_unseen=5))
        except Exception as exc:
            print(f"[EDGAR Poll Error] {exc}")

        try:
            candidates.extend(
                await self.sedar_source.fetch_latest_candidates(
                    max_unseen=2, force=force_sedar
                )
            )
        except Exception as exc:
            print(f"[SEDAR+ Poll Error] {exc}")

        sent_count = 0
        for filing, audit in candidates:
            # Stage 1: Free deterministic filter rules
            ok1, reason1 = self.filter_engine.evaluate_stage1_free(filing, audit)
            if not ok1:
                run = audit.finish(f"FILTERED_STAGE1 ({reason1})")
                self.store.save_audit(run)
                self.store.save_filing(filing, passed=False, audit_run_id=run.run_id)
                continue

            # Enrich Company Website via Exa + TypeSafe QC
            await self.pipeline.find_verified_company_website(filing, audit)

            # Filter out filing if Exa + TypeSafe QC did not find a verified website
            if not filing.company_website:
                reason_website = "Exa found no website passing TypeSafe QC"
                run = audit.finish(f"FILTERED_NO_WEBSITE ({reason_website})")
                self.store.save_audit(run)
                self.store.save_filing(filing, passed=False, audit_run_id=run.run_id)
                continue

            # Stage 2: TypeSafe AI semantic filter rules
            ok2, reason2 = await self.filter_engine.evaluate_stage2_semantic(
                filing, audit
            )
            if not ok2:
                run = audit.finish(f"FILTERED_STAGE2 ({reason2})")
                self.store.save_audit(run)
                self.store.save_filing(filing, passed=False, audit_run_id=run.run_id)
                continue

            run = audit.finish("PASSED_AND_SENT")
            self.store.save_audit(run)
            self.store.save_filing(filing, passed=True, audit_run_id=run.run_id)

            embed = build_filing_embed(filing, run)
            view = FilingAlertView(
                filing=filing,
                audit_run_id=run.run_id,
                store=self.store,
                filter_engine=self.filter_engine,
                pipeline=self.pipeline,
            )
            for dm in dms:
                try:
                    await dm.send(embed=embed, view=view)
                except Exception as exc:
                    print(f"[Warning] Failed to send alert to DM: {exc}", flush=True)

            sent_count += 1
            # Once a qualifying entry is sent in this cycle, break so each loop delivers the latest match cleanly
            break

        return sent_count

    async def on_message(self, message: discord.Message) -> None:
        """Handles natural-language DM commands strictly from OWNER_DISCORD_ID."""
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if not is_authorized_owner(message.author.id):
            return

        text = message.content.strip()
        lower = text.lower()

        # 0a. Start / Resume Polling Loop ("start", "resume", "unpause")
        if lower in ("start", "resume", "unpause", "go"):
            if self.poll_filings_loop.is_running():
                await message.reply("🟢 The Form D & SEDAR+ polling loop is already running!")
            else:
                interval = float(
                    self.store.get_setting(
                        "poll_interval_minutes", str(settings.DEFAULT_POLL_INTERVAL_MINUTES)
                    )
                )
                self.poll_filings_loop.change_interval(minutes=max(1.0, interval))
                self.poll_filings_loop.start()
                await message.reply(
                    f"🟢 **Started!** Polling SEC EDGAR & SEDAR+ every **{interval:g} minute(s)**."
                )
            return

        # 0b. Stop / Pause Polling Loop ("stop", "pause", "shutdown", "turn off")
        if lower in ("stop", "pause", "shutdown", "turn off"):
            if self.poll_filings_loop.is_running():
                self.poll_filings_loop.cancel()
                await message.reply(
                    "⏸️ **Stopped polling!** The bot is now idle ($0.00 cost) and waiting in standby. "
                    "Send `start` anytime in DMs to resume polling (or `kill` to exit the .exe completely)."
                )
            else:
                await message.reply(
                    "⏸️ Polling is already stopped. Send `start` anytime to resume."
                )
            return

        # 0c. Full Process Exit ("kill", "exit", "quit")
        if lower in ("kill", "exit", "quit"):
            await message.reply("🛑 Terminating FormDScoutBot.exe process completely. Goodbye!")
            await self.close()
            return

        # 1. Change loop interval ("interval 1", "run every 15 minutes", "loop 30")
        m_int = re.match(
            r"^(?:set\s+)?(?:interval|loop|run\s+every)\s+(\d+(?:\.\d+)?)\s*(?:m|min|minutes?)?$",
            lower,
        )
        if m_int:
            mins = max(1.0, float(m_int.group(1)))
            self.store.set_setting("poll_interval_minutes", str(mins))
            self.poll_filings_loop.change_interval(minutes=mins)
            await message.reply(
                f"⏱️ Loop interval updated to **every {mins:g} minute(s)**."
            )
            return

        # 2. Manual Poll Trigger ("poll", "poll now", "check now")
        if lower in ("poll", "poll now", "check now", "run now"):
            await message.reply("🔎 Running an immediate SEC EDGAR + SEDAR+ check...")
            sent = await self.run_single_poll_cycle(force_sedar=False)
            if sent == 0:
                latest_audit = self.store.get_audit()
                msg = "No new filings passed your criteria on this check."
                if latest_audit:
                    msg += f" (Latest evaluated: `{latest_audit.target_label}` -> `{latest_audit.outcome}`, cost `${latest_audit.total_cost_usd:.6f}`)"
                await message.reply(msg)
            return

        # 3. Audit Last Run / Inspect Exa Queries ("audit", "queries", "exa queries", "audit <id>")
        if lower.startswith("audit") or lower in ("queries", "exa queries", "exa"):
            parts = text.split(maxsplit=1)
            run_id = (
                parts[1].strip()
                if lower.startswith("audit") and len(parts) > 1 and parts[1].lower() != "last"
                else None
            )
            run = self.store.get_audit(run_id)
            if not run:
                await message.reply("No audit runs recorded yet.")
                return
            session = AuditSession(run.run_type, run.target_label)
            session.run = run
            await message.reply(session.format_discord_report()[:1990])
            return

        # 4. List or Remove Filter Rules ("rules", "remove rule 3")
        if lower in ("rules", "list rules", "criteria"):
            rules = self.store.list_rules()
            if not rules:
                await message.reply("No filter rules configured.")
                return
            formatted = "\n".join(
                f"`#{r.id}` [{r.stage.upper()}] **{r.raw_text}** (`{r.field} {r.operator} {r.value}`)"
                for r in rules
            )
            await message.reply(f"### 🛡️ Active Discriminating Criteria\n{formatted}")
            return

        m_rem = re.match(r"^(?:remove|delete)\s+rule\s+#?(\d+)$", lower)
        if m_rem:
            rid = int(m_rem.group(1))
            ok = self.store.remove_rule(rid)
            await message.reply(
                f"🗑️ Removed rule `#{rid}`." if ok else f"Rule `#{rid}` not found."
            )
            return

        # 5. Request More Info / Executive Deep-Dive by Name ("more info on Jane Doe", "deepdive John Smith")
        m_dd = re.match(
            r"^(?:more\s+info(?:\s+on|\s+about)?|deep\s*dive|enrich)\s*[:\-]?\s*(.+)$",
            text,
            re.I,
        )
        if m_dd:
            target_names_raw = m_dd.group(1).strip()
            latest = self.store.get_latest_passed_filing()
            if not latest:
                await message.reply("No recent qualifying filing found to deep-dive.")
                return
            filing, _ = latest
            requested = [
                n.strip().lower() for n in re.split(r",|and", target_names_raw) if n.strip()
            ]
            matched_execs = [
                e
                for e in filing.executives
                if any(req in e.full_name.lower() for req in requested)
            ]
            if not matched_execs and "all" in requested:
                matched_execs = filing.executives

            if not matched_execs:
                await message.reply(
                    f"Could not match `{target_names_raw}` against executives in **{filing.company_name}**: "
                    f"`{filing.executive_names_formatted}`"
                )
                return

            await message.reply(
                f"🕵️ Running Exa + TypeSafe QC + Apify email verification for "
                f"**{', '.join(e.full_name for e in matched_execs)}**..."
            )
            audit = AuditSession(
                run_type="executive_deep_dive",
                target_label=f"Deep-Dive ({filing.company_name})",
            )
            embeds = [
                build_executive_embed(
                    await self.pipeline.deep_dive_executive(e, filing, audit)
                )
                for e in matched_execs
            ]
            run = audit.finish("COMPLETED")
            self.store.save_audit(run)
            await message.reply(
                content=f"✅ Deep-dive finished (`${run.total_cost_usd:.5f}` total cost):",
                embeds=embeds[:10],
                view=AuditOnlyView(self.store, run.run_id),
            )
            return

        # 6. Add New Discriminating Criterion (e.g. "exclude filing type: debt")
        if any(
            lower.startswith(prefix)
            for prefix in (
                "exclude",
                "no ",
                "block ",
                "require ",
                "only ",
                "raised ",
                "min ",
                "max ",
                "rule:",
            )
        ):
            clean_rule = re.sub(r"^rule:\s*", "", text, flags=re.I)
            audit = AuditSession("rule_parse", f"Add rule: {clean_rule[:40]}")
            rule = await self.filter_engine.parse_and_add_user_rule(clean_rule, audit)
            run = audit.finish("ADDED_RULE")
            self.store.save_audit(run)
            await message.reply(
                f"✅ Added Discriminating Criterion `#{rule.id}`:\n"
                f"• **Raw:** `{rule.raw_text}`\n"
                f"• **Stage:** `{rule.stage}` (`{rule.field} {rule.operator} {rule.value}`)\n"
                f"• **Parse Cost:** `${run.total_cost_usd:.6f}`"
            )
            return

        await message.reply(
            "**Available DM Commands:**\n"
            "• `start` / `stop` — Start or stop the background Form D & SEDAR+ polling loop\n"
            "• `interval <minutes>` — Change how often the bot runs (e.g. `interval 1` or `interval 15`)\n"
            "• `exclude filing type: debt` — Add a new discriminating filter rule (any natural phrase works!)\n"
            "• `rules` / `remove rule <id>` — View or delete active filter criteria\n"
            "• `more info on <Executive Name>` — Deep-dive LinkedIn, socials, blogs, websites, & verified email\n"
            "• `audit` — Inspect every step, token count, and USD cost of the latest run\n"
            "• `poll now` — Trigger an immediate check\n"
            "• `kill` — Completely exit the background `.exe` process"
        )

    def _register_slash_commands(self) -> None:
        @self.tree.command(
            name="interval", description="Change how often the Form D & SEDAR+ loop runs (in minutes)"
        )
        async def slash_interval(interaction: discord.Interaction, minutes: float) -> None:
            if not is_authorized_owner(interaction.user.id):
                return
            mins = max(1.0, minutes)
            self.store.set_setting("poll_interval_minutes", str(mins))
            self.poll_filings_loop.change_interval(minutes=mins)
            await interaction.response.send_message(
                f"⏱️ Loop interval updated to **every {mins:g} minute(s)**."
            )

        @self.tree.command(
            name="audit", description="Inspect each step and cost (tokens & USD) of the latest bot run"
        )
        async def slash_audit(
            interaction: discord.Interaction, run_id: str | None = None
        ) -> None:
            if not is_authorized_owner(interaction.user.id):
                return
            run = self.store.get_audit(run_id)
            if not run:
                await interaction.response.send_message("No audit runs recorded yet.")
                return
            session = AuditSession(run.run_type, run.target_label)
            session.run = run
            await interaction.response.send_message(
                session.format_discord_report()[:1990]
            )


        @self.tree.command(
            name="start", description="Start or resume the Form D & SEDAR+ background polling loop"
        )
        async def slash_start(interaction: discord.Interaction) -> None:
            if not is_authorized_owner(interaction.user.id):
                return
            if self.poll_filings_loop.is_running():
                await interaction.response.send_message("🟢 Polling loop is already running!")
            else:
                interval = float(
                    self.store.get_setting(
                        "poll_interval_minutes", str(settings.DEFAULT_POLL_INTERVAL_MINUTES)
                    )
                )
                self.poll_filings_loop.change_interval(minutes=max(1.0, interval))
                self.poll_filings_loop.start()
                await interaction.response.send_message(
                    f"🟢 **Started!** Polling every **{interval:g} minute(s)**."
                )

        @self.tree.command(
            name="stop", description="Stop/pause the Form D & SEDAR+ polling loop (send /start to resume)"
        )
        async def slash_stop(interaction: discord.Interaction) -> None:
            if not is_authorized_owner(interaction.user.id):
                return
            if self.poll_filings_loop.is_running():
                self.poll_filings_loop.cancel()
                await interaction.response.send_message(
                    "⏸️ **Stopped polling!** Send `start` or `/start` anytime to resume."
                )
            else:
                await interaction.response.send_message("⏸️ Polling is already stopped.")


def main() -> None:
    if not settings.DISCORD_BOT_TOKEN or settings.OWNER_DISCORD_ID == 0:
        raise SystemExit(
            "ERROR: Please configure DISCORD_BOT_TOKEN and your numeric OWNER_DISCORD_ID in .env before starting."
        )
    bot = FormDScoutBot()
    bot.run(settings.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
