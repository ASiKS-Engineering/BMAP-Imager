"""Bmap Imager — a Raspberry Pi Imager style flashing tool.

Pure Python, no external engine: parses .bmap files and writes images
directly to Windows physical drives via the win32 API.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core import diskutil, flasher
from core.bmap import BmapError
from core.diskutil import DiskInfo, human_size
from core.flasher import FlashCancelled, FlashError, FlashProgress

APP_NAME = "Bmap Imager"
APP_VERSION = "2.0"
WINDOW_SIZE = "900x620"

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

        self.title("Speicher auswählen")
        self.geometry("620x480")
        self.transient(master)
        self.grab_set()
        self.resizable(False, True)

        self.selected: DiskInfo | None = None
        self.hide_system = ctk.BooleanVar(value=True)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkLabel(
            self, text="Ziellaufwerk auswählen", font=ctk.CTkFont(size=17, weight="bold")
        )
        header.grid(row=0, column=0, padx=18, pady=(18, 4), sticky="w")

        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.grid(row=1, column=0, padx=18, pady=(0, 10), sticky="ew")
        filter_row.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            filter_row,
            text="Systemlaufwerke ausblenden",
            variable=self.hide_system,
            command=self.populate,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(filter_row, text="Aktualisieren", width=110, command=self.populate).grid(
            row=0, column=1, padx=(8, 0)
        )

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, padx=18, pady=(0, 16), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            footer, text="Abbrechen", width=110, fg_color="gray40", command=self.destroy
        ).grid(row=0, column=1)

        self._all_drives: list[DiskInfo] = []
        self.populate()

    def populate(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()

        try:
            self._all_drives = diskutil.list_physical_drives()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            ctk.CTkLabel(self.list_frame, text=f"Fehler beim Auflisten: {exc}").grid(
                row=0, column=0, padx=8, pady=8, sticky="w"
            )
            return

        drives = self._all_drives

        if self.hide_system.get():
            drives = [d for d in drives if not d.is_system]

        if not drives:
            ctk.CTkLabel(
                self.list_frame, text="Keine passenden Laufwerke gefunden."
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

        select_text = "⚠ Trotzdem wählen" if drive.is_system else "Auswählen"
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
                "Systemlaufwerk",
                "Dies scheint ein Systemlaufwerk zu sein und kann Windows "
                "oder andere wichtige Daten enthalten.\n\n"
                f"PhysicalDrive{drive.number} ({drive.model}) wirklich als Ziel wählen?",
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
        self.minsize(860, 600)

        self.image_path: Path | None = None
        self.bmap_path: Path | None = None
        self.selected_drive: DiskInfo | None = None

        self.event_queue: "queue.Queue" = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.flash_thread: threading.Thread | None = None

        self.verify_var = ctk.BooleanVar(value=False)
        self._build_ui()
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_chooser_row()
        self._build_options_row()
        self._build_bottom_bar()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, padx=28, pady=(22, 4), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text=APP_NAME, font=ctk.CTkFont(size=26, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header, text=f"v{APP_VERSION}", text_color=COLOR_MUTED
        ).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            header,
            text="Raw- und .bmap-Images direkt auf ein Laufwerk schreiben",
            text_color=COLOR_MUTED,
        ).grid(row=1, column=0, columnspan=2, pady=(2, 0), sticky="w")

        ctk.CTkFrame(header, height=2, fg_color=COLOR_ACCENT).grid(
            row=2, column=0, columnspan=2, pady=(18, 0), sticky="ew"
        )

    def _build_chooser_row(self) -> None:
        chooser = ctk.CTkFrame(self, fg_color="transparent")
        chooser.grid(row=1, column=0, padx=28, pady=(20, 8), sticky="ew")
        chooser.grid_columnconfigure((0, 2), weight=1)

        self._build_image_card(chooser, 0)

        ctk.CTkLabel(
            chooser, text="→", font=ctk.CTkFont(size=24), text_color=COLOR_MUTED
        ).grid(row=0, column=1, padx=10)

        self.storage_card = self._make_choice_card(
            chooser, 2, "💾", "ZIEL", "Speicher auswählen", self.choose_storage
        )

    def _build_image_card(self, parent, column) -> None:
        card = ctk.CTkFrame(parent, corner_radius=14, height=285)
        card.grid(row=0, column=column, sticky="nsew", ipady=6)
        card.grid_propagate(False)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="🖴", font=ctk.CTkFont(size=30)).grid(
            row=0, column=0, rowspan=7, padx=(18, 6), pady=16, sticky="n"
        )

        ctk.CTkLabel(
            card, text="IMAGE", text_color=COLOR_MUTED, font=ctk.CTkFont(size=12, weight="bold")
        ).grid(row=0, column=1, padx=(0, 16), pady=(16, 2), sticky="w")

        self.image_value_label = ctk.CTkLabel(
            card, text="Image auswählen", font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=280, justify="left",
        )
        self.image_value_label.grid(row=1, column=1, padx=(0, 16), pady=(0, 4), sticky="w")

        self.image_detail_label = ctk.CTkLabel(
            card, text="", text_color=COLOR_MUTED, wraplength=280, justify="left"
        )
        self.image_detail_label.grid(row=2, column=1, padx=(0, 16), pady=(0, 8), sticky="w")

        ctk.CTkButton(card, text="Image durchsuchen", width=160, command=self.choose_image).grid(
            row=3, column=1, padx=(0, 16), pady=(0, 12), sticky="w"
        )

        ctk.CTkLabel(
            card, text=".bmap-Datei (optional)", text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=4, column=1, padx=(0, 16), pady=(0, 2), sticky="w")

        self.bmap_value_label = ctk.CTkLabel(
            card, text="Kein .bmap ausgewählt", wraplength=280, justify="left"
        )
        self.bmap_value_label.grid(row=5, column=1, padx=(0, 16), pady=(0, 8), sticky="w")

        bmap_buttons = ctk.CTkFrame(card, fg_color="transparent")
        bmap_buttons.grid(row=6, column=1, padx=(0, 16), pady=(0, 16), sticky="w")

        ctk.CTkButton(
            bmap_buttons, text="BMAP durchsuchen", width=160, command=self.choose_bmap
        ).grid(row=0, column=0)

        ctk.CTkButton(
            bmap_buttons, text="Entfernen", width=90, fg_color="gray40", command=self.clear_bmap
        ).grid(row=0, column=1, padx=(8, 0))

    def _make_choice_card(self, parent, column, icon, title, placeholder, command):
        card = ctk.CTkFrame(parent, corner_radius=14, height=285)
        card.grid(row=0, column=column, sticky="nsew", ipady=6)
        card.grid_propagate(False)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text=icon, font=ctk.CTkFont(size=30)).grid(
            row=0, column=0, rowspan=4, padx=(18, 6), pady=16, sticky="n"
        )

        ctk.CTkLabel(
            card, text=title, text_color=COLOR_MUTED, font=ctk.CTkFont(size=12, weight="bold")
        ).grid(row=0, column=1, padx=(0, 16), pady=(16, 2), sticky="w")

        value_label = ctk.CTkLabel(
            card,
            text=placeholder,
            font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=280,
            justify="left",
        )
        value_label.grid(row=1, column=1, padx=(0, 16), pady=(0, 4), sticky="w")

        detail_label = ctk.CTkLabel(
            card, text="", text_color=COLOR_MUTED, wraplength=280, justify="left"
        )
        detail_label.grid(row=2, column=1, padx=(0, 16), pady=(0, 8), sticky="w")

        ctk.CTkButton(card, text="Durchsuchen", width=130, command=command).grid(
            row=3, column=1, padx=(0, 16), pady=(0, 16), sticky="w"
        )

        card.value_label = value_label
        card.detail_label = detail_label
        return card

    def _build_options_row(self) -> None:
        options = ctk.CTkFrame(self, fg_color="transparent")
        options.grid(row=2, column=0, padx=28, pady=(4, 8), sticky="ew")
        options.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            options,
            text="Prüfsummen aus .bmap beim Schreiben verifizieren",
            variable=self.verify_var,
        ).grid(row=0, column=0, sticky="w")

    def _build_bottom_bar(self) -> None:
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=4, column=0, padx=28, pady=(4, 24), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bottom, height=18, corner_radius=9)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(bottom, text="Bereit", anchor="w")
        self.status_label.grid(row=1, column=0, sticky="w")

        self.write_button = ctk.CTkButton(
            bottom,
            text="SCHREIBEN",
            width=170,
            height=48,
            font=ctk.CTkFont(size=15, weight="bold"),
            state="disabled",
            command=self.start_flash,
        )
        self.write_button.grid(row=0, column=1, rowspan=2, padx=(16, 0))

        self.cancel_button = ctk.CTkButton(
            bottom,
            text="Abbrechen",
            width=110,
            height=48,
            fg_color=COLOR_DANGER,
            hover_color=COLOR_DANGER_HOVER,
            command=self.cancel_flash,
        )
        self.cancel_button.grid(row=0, column=2, rowspan=2, padx=(10, 0))
        self.cancel_button.grid_remove()

    # ------------------------------------------------------------------
    # Image / storage selection
    # ------------------------------------------------------------------

    def choose_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Image auswählen",
            filetypes=[
                ("Disk images", "*.img *.raw *.bin"),
                ("Alle Dateien", "*.*"),
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
            title=".bmap-Datei auswählen",
            filetypes=[("BMAP-Dateien", "*.bmap"), ("Alle Dateien", "*.*")],
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
            detail = f"{size} — Sparse-Write über .bmap"
        else:
            detail = f"{size} — vollständiger Raw-Write"

        self.image_detail_label.configure(text=detail)

    def _refresh_bmap_labels(self) -> None:
        if self.bmap_path:
            self.bmap_value_label.configure(text=self.bmap_path.name)
        else:
            self.bmap_value_label.configure(text="Kein .bmap ausgewählt")

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
        self._update_write_button()

    def _update_write_button(self) -> None:
        ready = self.image_path is not None and self.selected_drive is not None
        self.write_button.configure(state="normal" if ready else "disabled")

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
                "Das Image ist größer als das Ziellaufwerk.\n\n"
                f"Image: {human_size(image_size)}\n"
                f"Ziel:  {human_size(self.selected_drive.size)}",
            )
            return

        warning = (
            "Alle Daten auf dem gewählten Laufwerk werden gelöscht.\n\n"
            f"Image: {self.image_path}\n"
            f"Ziel:  PhysicalDrive{self.selected_drive.number} "
            f"({human_size(self.selected_drive.size)})\n\n"
            "Zum Fortfahren unten YES eingeben."
        )

        answer = ctk.CTkInputDialog(text=warning, title="Schreibvorgang bestätigen")
        if answer.get_input() != "YES":
            return

        self.write_button.configure(state="disabled")
        self.cancel_button.grid()
        self.progress.set(0)
        self.status_label.configure(text="Starte…")
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
            self.status_label.configure(text="Wird abgebrochen…")

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
                f"Schreibe — {percent:.1f}% "
                f"({human_size(progress.bytes_written)} / {human_size(progress.total_bytes)}) "
                f"— Bereich {progress.current_range}/{progress.total_ranges}"
            )
        )

    def _on_finished(self, *, success: bool, cancelled: bool = False, error: str | None = None) -> None:
        self.flash_thread = None
        self.cancel_event = None
        self.write_button.configure(state="normal")
        self.cancel_button.grid_remove()

        if success:
            self.progress.set(1)
            self.status_label.configure(text="Schreibvorgang erfolgreich abgeschlossen")
            messagebox.showinfo(APP_NAME, "Das Image wurde erfolgreich geschrieben.")
        elif cancelled:
            self.status_label.configure(text="Abgebrochen")
        else:
            self.status_label.configure(text="Schreibvorgang fehlgeschlagen")
            messagebox.showerror(APP_NAME, f"Schreibvorgang fehlgeschlagen:\n\n{error}")


if __name__ == "__main__":
    app = BmapFlashApp()
    app.mainloop()
