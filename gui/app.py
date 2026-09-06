"""Bmap Imager — a Raspberry Pi Imager style flashing tool.

Pure Python, no external engine: parses .bmap files and writes images
directly to Windows physical drives via the win32 API.
"""
from __future__ import annotations

import ctypes
import queue
import json
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core import diskutil, flasher
from core.bmap import BmapError
from core.diskutil import DiskError, DiskInfo, human_size
from core.flasher import FlashCancelled, FlashError, FlashProgress

APP_NAME = "Bmap Imager"
APP_AUTHOR = "created by ASiKS-Engineering"
WINDOW_SIZE = "820x430"
WINDOW_MIN_SIZE = (780, 400)


def _read_app_version() -> str:
    """Read the project version from the bundled or source checkout file."""
    candidates = [Path(__file__).resolve().parents[1] / "VERSION"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys._MEIPASS) / "VERSION")

    for version_path in candidates:
        try:
            version = version_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if version:
            return version

    return "unbekannt"


APP_VERSION = _read_app_version()


def _load_translations() -> dict[str, dict[str, str]]:
    candidates = [Path(__file__).resolve().parent / "translations.json"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys._MEIPASS) / "translations.json")

    for translations_path in candidates:
        try:
            with translations_path.open(encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
    return {"de": {}}


TRANSLATIONS = _load_translations()
LANGUAGE = "de"


def tr(key: str, **values) -> str:
    text = TRANSLATIONS.get(LANGUAGE, TRANSLATIONS.get("de", {})).get(key, key)
    return text.format(**values)


def _has_admin_rights() -> bool:
    if sys.platform != "win32":
        return True

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False

COLOR_ACCENT = "#1f6aa5"
COLOR_DANGER = "#a83232"
COLOR_DANGER_HOVER = "#832525"
COLOR_WARN_BADGE = "#c0392b"
COLOR_REMOVABLE_BADGE = "#2fa84f"
COLOR_FIXED_BADGE = "#b5541c"
COLOR_MUTED = ("gray45", "gray70")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class StorageDialog(ctk.CTkToplevel):
    """Modal list of physical drives, in the style of Raspberry Pi Imager."""

    def __init__(self, master: "BmapFlashApp"):
        super().__init__(master)

        self.title(tr("storage_select"))
        self.geometry("620x480")
        self.transient(master)
        self.grab_set()
        self.resizable(False, True)

        self.selected: DiskInfo | None = None
        self.hide_system = ctk.BooleanVar(value=True)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkLabel(
            self, text=tr("storage_header"), font=ctk.CTkFont(size=17, weight="bold")
        )
        header.grid(row=0, column=0, padx=18, pady=(18, 4), sticky="w")

        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.grid(row=1, column=0, padx=18, pady=(0, 10), sticky="ew")
        filter_row.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            filter_row,
            text=tr("hide_system"),
            variable=self.hide_system,
            command=self.populate,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(filter_row, text=tr("refresh"), width=110, command=self.populate).grid(
            row=0, column=1, padx=(8, 0)
        )

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, padx=18, pady=(0, 16), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            footer, text=tr("cancel"), width=110, fg_color="gray40", command=self.destroy
        ).grid(row=0, column=1)

        self._all_drives: list[DiskInfo] = []
        self.populate()

    def populate(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()

        try:
            self._all_drives = diskutil.list_physical_drives()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            ctk.CTkLabel(self.list_frame, text=tr("list_error", error=exc)).grid(
                row=0, column=0, padx=8, pady=8, sticky="w"
            )
            return

        drives = self._all_drives

        if self.hide_system.get():
            drives = [d for d in drives if not d.is_system]

        if not drives:
            ctk.CTkLabel(
                self.list_frame, text=tr("no_drives")
            ).grid(row=0, column=0, padx=8, pady=8, sticky="w")
            return

        for row, drive in enumerate(drives):
            self._add_drive_row(row, drive)

    def _add_drive_row(self, row: int, drive: DiskInfo) -> None:
        card = ctk.CTkFrame(self.list_frame, corner_radius=10)
        card.grid(row=row, column=0, padx=4, pady=5, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        if drive.is_system:
            badge_color, badge_text = COLOR_WARN_BADGE, "SYSTEM"
        elif drive.removable:
            badge_color, badge_text = COLOR_REMOVABLE_BADGE, "Wechseldatenträger"
        else:
            badge_color, badge_text = COLOR_FIXED_BADGE, "Festplatte"

        ctk.CTkLabel(
            card,
            text=badge_text,
            fg_color=badge_color,
            corner_radius=6,
            width=100,
            text_color="white",
        ).grid(row=0, column=0, rowspan=2, padx=12, pady=12)

        ctk.CTkLabel(
            card, text=drive.model, font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=1, padx=(0, 10), pady=(12, 0), sticky="w")

        details = f"PhysicalDrive{drive.number} — {human_size(drive.size)} — {drive.bus}"
        if drive.volumes:
            details += f" — {', '.join(drive.volumes)}"

        ctk.CTkLabel(card, text=details, text_color=COLOR_MUTED).grid(
            row=1, column=1, padx=(0, 10), pady=(0, 12), sticky="w"
        )

        select_text = tr("select_system") if drive.is_system else tr("select")
        select_color = COLOR_DANGER if drive.is_system else COLOR_ACCENT

        ctk.CTkButton(
            card,
            text=select_text,
            width=140,
            fg_color=select_color,
            command=lambda d=drive: self._select(d),
        ).grid(row=0, column=2, rowspan=2, padx=12, pady=12)

    def _select(self, drive: DiskInfo) -> None:
        if drive.is_system:
            confirmed = messagebox.askyesno(
                tr("system_drive"),
                tr("system_warning", drive=f"PhysicalDrive{drive.number}", model=drive.model),
                icon="warning",
            )

            if not confirmed:
                return

        self.selected = drive
        self.destroy()


class BmapFlashApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(APP_NAME)
        self.geometry(WINDOW_SIZE)
        self.minsize(*WINDOW_MIN_SIZE)

        self.image_path: Path | None = None
        self.bmap_path: Path | None = None
        self.selected_drive: DiskInfo | None = None

        self.event_queue: "queue.Queue" = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.flash_thread: threading.Thread | None = None

        self.verify_var = ctk.BooleanVar(value=False)
        self.appearance_mode = "dark"
        self._build_ui()
        self.after(100, self._poll_queue)
        self.after(150, self._check_admin_rights)

    def _check_admin_rights(self) -> None:
        if not _has_admin_rights():
            messagebox.showwarning(APP_NAME, tr("admin_warning"), parent=self)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_chooser_row()
        self._build_bottom_bar()

    def _rebuild_ui(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()
        if self.image_path is not None:
            self._refresh_image_labels()
            self._refresh_bmap_labels()
        if self.selected_drive is not None:
            self.storage_card.value_label.configure(text=self.selected_drive.model)

    def _change_language(self, language: str) -> None:
        global LANGUAGE
        LANGUAGE = language
        self._rebuild_ui()

    def _change_appearance(self, appearance: str) -> None:
        appearance_values = {tr("dark"): "dark", tr("light"): "light", tr("system"): "system"}
        self.appearance_mode = appearance_values[appearance]
        ctk.set_appearance_mode(self.appearance_mode)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, padx=24, pady=(16, 2), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text=APP_NAME, font=ctk.CTkFont(size=26, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        settings = ctk.CTkFrame(header, fg_color="transparent")
        settings.grid(row=0, column=2, sticky="e")
        ctk.CTkLabel(
            settings, text=tr("language"), font=ctk.CTkFont(size=10),
            text_color=COLOR_MUTED,
        ).grid(row=0, column=0, padx=(0, 4))
        language_menu = ctk.CTkOptionMenu(
            settings, values=["DE", "EN"], width=52, height=26,
            font=ctk.CTkFont(size=11),
            command=lambda value: self._change_language(value.lower()),
        )
        language_menu.set(LANGUAGE.upper())
        language_menu.grid(row=0, column=1, padx=(0, 8))
        ctk.CTkLabel(
            settings, text=tr("appearance"), font=ctk.CTkFont(size=10),
            text_color=COLOR_MUTED,
        ).grid(row=0, column=2, padx=(0, 4))
        appearance_menu = ctk.CTkOptionMenu(
            settings, values=[tr("dark"), tr("light"), tr("system")],
            width=86, height=26, font=ctk.CTkFont(size=11),
            command=self._change_appearance,
        )
        appearance_menu.set(tr(self.appearance_mode))
        appearance_menu.grid(row=0, column=3)

        ctk.CTkLabel(
            header,
            text=f"{tr('subtitle')} · {APP_AUTHOR}",
            text_color=COLOR_MUTED,
        ).grid(row=1, column=0, columnspan=3, pady=(2, 0), sticky="w")

        ctk.CTkFrame(header, height=2, fg_color=COLOR_ACCENT).grid(
            row=2, column=0, columnspan=3, pady=(12, 0), sticky="ew"
        )

    def _build_chooser_row(self) -> None:
        chooser = ctk.CTkFrame(self, fg_color="transparent")
        chooser.grid(row=1, column=0, padx=24, pady=(12, 0), sticky="ew")
        chooser.grid_columnconfigure((0, 2), weight=1, uniform="cards")

        self._build_image_card(chooser, 0)

        ctk.CTkLabel(
            chooser, text="→", font=ctk.CTkFont(size=24), text_color=COLOR_MUTED
        ).grid(row=0, column=1, padx=10)

        target_column = ctk.CTkFrame(chooser, fg_color="transparent")
        target_column.grid(row=0, column=2, sticky="nsew")
        target_column.grid_columnconfigure(0, weight=1)
        target_column.grid_rowconfigure(0, weight=1)

        self.storage_card = self._make_choice_card(
            target_column, 0, "💾", tr("target"), tr("no_storage"), self.choose_storage
        )

        actions = ctk.CTkFrame(target_column, fg_color="transparent")
        actions.grid(row=1, column=0, pady=(14, 0), sticky="ew")
        actions.grid_columnconfigure((0, 1), weight=1)

        self._action_mode = "write"
        self.action_button = ctk.CTkButton(
            actions,
            text=tr("write"),
            width=150,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            state="disabled",
            command=self.start_flash,
        )
        self.action_button.grid(row=0, column=0, columnspan=2, sticky="ew")

    def _build_image_card(self, parent, column) -> None:
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=0, column=column, sticky="nsew", ipady=6)
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=0, minsize=148)
        card.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, padx=18, pady=(14, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            header, text="🖴", font=ctk.CTkFont(size=24)
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        ctk.CTkLabel(
            header, text=tr("image"), text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12, weight="bold")
        ).grid(row=0, column=1, sticky="w")

        image_row = ctk.CTkFrame(card, fg_color="transparent")
        image_row.grid(row=1, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="ew")
        image_row.grid_columnconfigure(0, weight=1)
        self.image_value_label = ctk.CTkLabel(
            image_row, text=tr("no_image"),
            wraplength=200, justify="left", anchor="w",
        )
        self.image_value_label.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            image_row, text=tr("image_button"), width=110, height=32, command=self.choose_image
        ).grid(row=0, column=1, padx=(12, 0), sticky="e")

        self.image_detail_label = ctk.CTkLabel(
            card, text="", text_color=COLOR_MUTED, wraplength=360,
            justify="left", anchor="w"
        )
        self.image_detail_label.grid(row=2, column=0, columnspan=2, padx=18, pady=(0, 14), sticky="w")

        bmap_row = ctk.CTkFrame(card, fg_color="transparent")
        bmap_row.grid(row=4, column=0, columnspan=2, padx=18, pady=(0, 14), sticky="ew")
        bmap_row.grid_columnconfigure(0, weight=1)
        self.bmap_value_label = ctk.CTkLabel(
            bmap_row, text=tr("no_bmap"), wraplength=200,
            justify="left", anchor="w"
        )
        self.bmap_value_label.grid(row=0, column=0, sticky="w")
        self.bmap_button = ctk.CTkButton(
            bmap_row, text=tr("bmap_button"), width=110, height=32,
            command=self.choose_bmap,
        )
        self.bmap_button.grid(row=0, column=1, padx=(12, 0), sticky="e")

        ctk.CTkCheckBox(card, text=tr("verify"), variable=self.verify_var).grid(
            row=5, column=0, columnspan=2, padx=18, pady=(0, 0), sticky="w"
        )

    def _make_choice_card(self, parent, column, icon, title, placeholder, command):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=0, column=column, sticky="nsew", ipady=6)
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=0)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, padx=18, pady=(14, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            header, text=icon, width=28, height=24, corner_radius=6,
            fg_color="transparent", text_color="white",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")

        ctk.CTkLabel(
            header, text=title, text_color=COLOR_MUTED, font=ctk.CTkFont(size=12, weight="bold")
        ).grid(row=0, column=1, sticky="w")

        text_row = ctk.CTkFrame(card, fg_color="transparent")
        text_row.grid(row=1, column=0, columnspan=2, padx=18, pady=(0, 4), sticky="ew")
        text_row.grid_columnconfigure(0, weight=1)

        value_label = ctk.CTkLabel(
            text_row,
            text=placeholder,
            justify="left",
            anchor="w",
        )
        value_label.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(text_row, text=tr("browse"), width=110, height=32, command=command).grid(
            row=0, column=1, padx=(12, 0), sticky="e"
        )

        detail_label = ctk.CTkLabel(
            card, text="", text_color=COLOR_MUTED,
            justify="left", anchor="w"
        )
        detail_label.grid(row=2, column=0, columnspan=2, padx=18, pady=(0, 4), sticky="ew")

        hint_label = ctk.CTkLabel(
            card, text=tr("storage_hint"), text_color=COLOR_MUTED,
            justify="left", anchor="w",
        )
        hint_label.grid(row=3, column=0, columnspan=2, padx=18, pady=(0, 12), sticky="ew")

        # Make long texts wrap at the actual card width instead of a
        # hardcoded pixel guess; this avoids clipped or wasted space.
        def _update_wrap(event: "ctk.tkinter.Event") -> None:
            wrap = max(event.width - 56, 80)  # padding + safety margin
            value_label.configure(wraplength=max(wrap - 140, 80))  # minus button + gap
            detail_label.configure(wraplength=wrap)
            hint_label.configure(wraplength=wrap)

        card.bind("<Configure>", _update_wrap)

        card.value_label = value_label
        card.detail_label = detail_label
        card.hint_label = hint_label
        return card

    def _build_options_row(self) -> None:
        pass


    def _build_bottom_bar(self) -> None:
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, padx=24, pady=(0, 10), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bottom, height=12, corner_radius=6)
        self.progress.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 4))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(bottom, text=tr("ready"), anchor="w", text_color=COLOR_MUTED)
        self.status_label.grid(row=3, column=0, columnspan=3, sticky="w")

        ctk.CTkLabel(
            bottom, text=f"v{APP_VERSION}", text_color=COLOR_MUTED
        ).grid(row=3, column=3, padx=(10, 0), pady=(0, 0), sticky="e")

    # ------------------------------------------------------------------
    # Image / storage selection
    # ------------------------------------------------------------------

    def choose_image(self) -> None:
        path = filedialog.askopenfilename(
            title=tr("image_select"),
            filetypes=[
                (tr("image_select"), "*.img *.raw *.bin"),
                (tr("browse"), "*.*"),
            ],
        )

        if not path:
            return

        self.image_path = Path(path)

        # Auto-detect a sibling .bmap, but always leave the final choice to the user.
        candidate = Path(path + ".bmap")
        self.bmap_path = candidate if candidate.exists() else None

        self._refresh_image_labels()
        self._refresh_bmap_labels()
        self._update_write_button()

    def choose_bmap(self) -> None:
        path = filedialog.askopenfilename(
            title=tr("bmap_select"),
            filetypes=[("BMAP", "*.bmap"), (tr("browse"), "*.*")],
        )

        if not path:
            return

        self.bmap_path = Path(path)
        self._refresh_image_labels()
        self._refresh_bmap_labels()

    def clear_bmap(self) -> None:
        self.bmap_path = None
        self._refresh_image_labels()
        self._refresh_bmap_labels()

    def _refresh_image_labels(self) -> None:
        if self.image_path is None:
            return

        size = human_size(self.image_path.stat().st_size)
        self.image_value_label.configure(text=self.image_path.name)
        if self.bmap_path:
            detail = f"{size} — {tr('sparse_write')}"
        else:
            detail = f"{size} — {tr('raw_write')}"

        self.image_detail_label.configure(text=detail)

    def _refresh_bmap_labels(self) -> None:
        if self.bmap_path:
            self.bmap_value_label.configure(text=self.bmap_path.name)
            self.bmap_button.configure(
                text=tr("remove"), fg_color="gray40", hover_color="gray30",
                command=self.clear_bmap,
            )
        else:
            self.bmap_value_label.configure(text=tr("no_bmap"))
            self.bmap_button.configure(
                text=tr("bmap_button"),
                fg_color=("#3B8ED0", "#1F6AA5"), hover_color=("#36719F", "#144870"),
                command=self.choose_bmap,
            )

    def choose_storage(self) -> None:
        dialog = StorageDialog(self)
        self.wait_window(dialog)

        if dialog.selected is not None:
            self.selected_drive = dialog.selected
            self.storage_card.value_label.configure(text=self.selected_drive.model)
            details = (
                f"PhysicalDrive{self.selected_drive.number} — "
                f"{human_size(self.selected_drive.size)}"
            )
            if self.selected_drive.is_system:
                details += " — ⚠ SYSTEMLAUFWERK"
            self.storage_card.detail_label.configure(text=details)
            self.storage_card.hint_label.grid_remove()
        self._update_write_button()

    def _update_write_button(self) -> None:
        if self._action_mode != "write":
            return
        ready = self.image_path is not None and self.selected_drive is not None
        self.action_button.configure(state="normal" if ready else "disabled")

    def _set_action_mode(self, mode: str) -> None:
        """Switch the multi-function button between write / cancel / eject."""
        self._action_mode = mode

        if mode == "cancel":
            self.action_button.configure(
                text=tr("cancel"),
                fg_color=COLOR_DANGER,
                hover_color=COLOR_DANGER_HOVER,
                state="normal",
                command=self.cancel_flash,
            )
            return

        self.action_button.configure(
            fg_color=("#3B8ED0", "#1F6AA5"),
            hover_color=("#36719F", "#144870"),
        )

        if mode == "eject":
            self.action_button.configure(
                text=tr("eject"), state="normal", command=self.eject_storage
            )
        else:
            self.action_button.configure(text=tr("write"), command=self.start_flash)
            self._update_write_button()

    # ------------------------------------------------------------------
    # Flashing
    # ------------------------------------------------------------------

    def start_flash(self) -> None:
        if self.flash_thread is not None:
            return

        assert self.image_path is not None
        assert self.selected_drive is not None

        image_size = self.image_path.stat().st_size

        if image_size > self.selected_drive.size:
            messagebox.showerror(
                APP_NAME,
                tr(
                    "image_too_large",
                    image=human_size(image_size),
                    target=human_size(self.selected_drive.size),
                ),
            )
            return

        warning = (
            tr(
                "confirm_warning",
                image=self.image_path,
                number=self.selected_drive.number,
                size=human_size(self.selected_drive.size),
            )
        )

        answer = ctk.CTkInputDialog(text=warning, title=tr("confirm_title"))
        if answer.get_input() != "YES":
            return

        self._set_action_mode("cancel")
        self.progress.set(0)
        self.status_label.configure(text=tr("starting"))
        self.cancel_event = threading.Event()

        self.flash_thread = threading.Thread(
            target=self._flash_worker,
            args=(self.image_path, self.bmap_path, self.selected_drive.number, self.cancel_event),
            daemon=True,
        )
        self.flash_thread.start()

    def cancel_flash(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.action_button.configure(state="disabled")
            self.status_label.configure(text=tr("cancelling"))

    def eject_storage(self) -> None:
        if self.selected_drive is None:
            return

        try:
            diskutil.eject_drive(self.selected_drive.number)
        except (DiskError, OSError) as exc:
            messagebox.showerror(APP_NAME, tr("eject_error", error=exc))
            return

        self.selected_drive = None
        self.storage_card.value_label.configure(text=tr("no_storage"))
        self.storage_card.detail_label.configure(text="")
        self.storage_card.hint_label.grid()
        self._set_action_mode("write")
        self.status_label.configure(text=tr("eject_success"))

    def _flash_worker(self, image_path, bmap_path, disk_number, cancel_event) -> None:
        try:
            flasher.flash(
                str(image_path),
                str(bmap_path) if bmap_path else None,
                disk_number,
                verify=self.verify_var.get(),
                progress=lambda p: self.event_queue.put(("progress", p)),
                cancel_event=cancel_event,
            )
            self.event_queue.put(("done", None))
        except FlashCancelled:
            self.event_queue.put(("cancelled", None))
        except (FlashError, BmapError) as exc:
            self.event_queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.event_queue.put(("error", f"unerwarteter Fehler: {exc}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()

                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "done":
                    self._on_finished(success=True)
                elif kind == "cancelled":
                    self._on_finished(success=False, cancelled=True)
                elif kind == "error":
                    self._on_finished(success=False, error=payload)
        except queue.Empty:
            pass

        self.after(100, self._poll_queue)

    def _on_progress(self, progress: FlashProgress) -> None:
        self.progress.set(progress.fraction)
        percent = progress.fraction * 100
        self.status_label.configure(
            text=(
                tr(
                    "writing",
                    percent=percent,
                    written=human_size(progress.bytes_written),
                    total=human_size(progress.total_bytes),
                    current=progress.current_range,
                    ranges=progress.total_ranges,
                )
            )
        )

    def _on_finished(self, *, success: bool, cancelled: bool = False, error: str | None = None) -> None:
        self.flash_thread = None
        self.cancel_event = None

        if success:
            self.progress.set(1)
            self.status_label.configure(text=tr("success"))
            self._set_action_mode("eject")
            messagebox.showinfo(APP_NAME, tr("success_message"))
        elif cancelled:
            self.status_label.configure(text=tr("cancelled"))
            self._set_action_mode("write")
        else:
            self.status_label.configure(text=tr("failed"))
            self._set_action_mode("write")
            messagebox.showerror(APP_NAME, tr("failed_message", error=error))


if __name__ == "__main__":
    app = BmapFlashApp()
    app.mainloop()
