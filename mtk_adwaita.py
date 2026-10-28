#!/usr/bin/env python3
"""GNOME-native frontend for the existing MTK device connection backend."""

import logging
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

from app_settings import (get_advanced_logging, get_backend_dir,
                          set_advanced_logging, set_backend_dir)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango


APP_ID = "io.github.mtkclient.Adwaita"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_LOG_CHARS = 200_000
MTKCLIENT_DIR = os.environ.get(
    "MTKCLIENT_DIR",
    get_backend_dir(os.path.join(APP_DIR, "..", "mtkclient")),
)


class UiLogHandler(logging.Handler):
    """Forward backend log records to the GTK log view on the main thread."""

    def __init__(self, window):
        super().__init__(logging.DEBUG)
        self.window = window

    def emit(self, record):
        message = self.format(record)
        GLib.idle_add(self.window.append_log, message)


class BackendLog:
    """Adapter used by MTK's GUI logging hooks; writes to terminal and UI."""

    def __init__(self, window):
        self.window = window

    def emit(self, message):
        text = str(message)
        GLib.idle_add(self.window.append_log, text)


class BackendProgress:
    """Adapt MTK's byte-position callback to the GTK progress display."""

    def __init__(self, window):
        self.window = window

    def emit(self, position):
        GLib.idle_add(self.window._update_operation_progress, position)


class MtkWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MTK Client")
        self.set_default_size(900, 650)
        self.backend = None
        self.connection_thread = None
        self.operation_thread = None
        self.device_connected = False
        self.operation_busy = False
        self.operation_started_at = None
        self.operation_total = None
        self.operation_progress_position = 0
        self.operation_has_progress = False
        self.loader_path = None
        self.preloader_path = None
        self.partitions = []
        self.partition_checks = {}
        self.partition_rows = {}
        self.partition_images = {}
        self.log_buffer = Gtk.TextBuffer()

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        self.stack.set_vhomogeneous(False)
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        preferences_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        preferences_button.set_tooltip_text("Preferences")
        preferences_button.update_property([Gtk.AccessibleProperty.LABEL], ["Preferences"])
        preferences_button.connect("clicked", self.open_preferences)
        header.pack_end(preferences_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        connection_page = self._connection_page()
        self.stack.add_titled_with_icon(connection_page, "device", "Connect",
                                        "phone-symbolic")
        header.pack_end(self.connection_options_button)
        self.stack.add_titled_with_icon(self._partition_page(), "partitions", "Flash",
                                        "drive-harddisk-symbolic")
        self.stack.add_titled_with_icon(self._utilities_page(), "utilities", "Utilities",
                                        "applications-system-symbolic")
        self.stack.connect("notify::visible-child-name", self._update_connection_options_visibility)
        self._update_connection_options_visibility(self.stack, None)

    def open_connection_guide(self, *_args):
        """Present an animated, illustrated connection walkthrough."""
        if getattr(self, "connection_guide", None):
            self.connection_guide.present()
            return

        window = Adw.Window(
            transient_for=self, modal=True, title="Connection guide",
            default_width=920, default_height=760,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        eyebrow = Gtk.Label(label="DEVICE SETUP", xalign=0)
        eyebrow.add_css_class("caption")
        eyebrow.add_css_class("accent")
        intro.append(eyebrow)
        title = Gtk.Label(label="Let’s get your device connected", xalign=0)
        title.add_css_class("title-1")
        intro.append(title)
        subtitle = Gtk.Label(
            label="Follow the illustrated steps. Start with the phone powered off and USB unplugged.",
            xalign=0, wrap=True,
        )
        subtitle.add_css_class("dimmed")
        intro.append(subtitle)
        page.append(intro)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        step_count = Gtk.Label(xalign=0)
        step_count.add_css_class("heading")
        progress_row.append(step_count)
        progress_segments = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        progress_segments.set_hexpand(True)
        progress_bars = []
        for index in range(3):
            segment = Gtk.ProgressBar()
            segment.set_hexpand(True)
            segment.set_size_request(-1, 8)
            segment.set_accessible_role(Gtk.AccessibleRole.PROGRESS_BAR)
            progress_segments.append(segment)
            progress_bars.append(segment)
        progress_row.append(progress_segments)
        page.append(progress_row)

        # Each page gives its theme-aware SVG illustration room to read at a glance;
        # Gtk.Stack supplies a native slide transition between the steps.
        slides = Gtk.Stack()
        slides.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        slides.set_transition_duration(360)
        slides.set_vhomogeneous(False)

        art_pictures = []
        mode_pictures = []
        preview_popovers = []
        hover_close_sources = []
        pulse_phase = False
        style_manager = Adw.StyleManager.get_default()
        art_palettes = {
            False: {
                "outline": "#5e5c64", "device": "#deddda", "screen": "#f6f5f4",
                "soft": "#9a9996", "accent": "#3584e4", "accent_soft": "#99c1f1",
                "board": "#e8e6e3", "trace": "#9a9996", "pad": "#c0bfbc",
                "label_bg": "#deddda", "text": "#3d3846",
            },
            True: {
                "outline": "#c0bfbc", "device": "#3d3846", "screen": "#241f31",
                "soft": "#77767b", "accent": "#78aeed", "accent_soft": "#355b83",
                "board": "#303030", "trace": "#77767b", "pad": "#5e5c64",
                "label_bg": "#3d3846", "text": "#f6f5f4",
            },
        }

        def render_illustration(
            picture, art_name, pulse=False, raster_size=(720, 960)
        ):
            dark = style_manager.get_dark()
            palette = dict(art_palettes[dark])
            if pulse and pulse_phase:
                palette["accent"] = "#a8d0fb" if dark else "#6ba8f0"
            path = os.path.join(APP_DIR, "assets", art_name)
            with open(path, "rb") as svg_file:
                svg = svg_file.read().decode("utf-8")
            color_map = {
                light_value: palette[name]
                for name, light_value in art_palettes[False].items()
            }
            svg = re.sub(
                r"#[0-9a-fA-F]{6}",
                lambda match: color_map.get(match.group(0), match.group(0)),
                svg,
            )
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.set_size(*raster_size)
            loader.write(svg.encode("utf-8"))
            loader.close()
            picture.set_paintable(Gdk.Texture.new_for_pixbuf(loader.get_pixbuf()))

        def update_illustration_theme(*_args):
            for picture, art_name, raster_size in art_pictures:
                render_illustration(
                    picture, art_name,
                    pulse=any(mode_picture is picture
                              for mode_picture, _, _ in mode_pictures) and pulse_phase,
                    raster_size=raster_size,
                )

        def pulse_mode_actions():
            nonlocal pulse_phase
            if slides.get_visible_child_name() != "mode":
                return GLib.SOURCE_CONTINUE
            pulse_phase = not pulse_phase
            for picture, art_name, raster_size in mode_pictures:
                render_illustration(
                    picture, art_name, pulse=True, raster_size=raster_size
                )
            return GLib.SOURCE_CONTINUE

        self._connection_guide_theme_handler = style_manager.connect(
            "notify::dark", update_illustration_theme
        )

        def slide(art_name, art_label, heading, description, extra=None):
            layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
            layout.set_valign(Gtk.Align.CENTER)

            if art_name:
                picture = Gtk.Picture()
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_can_shrink(True)
                picture.set_size_request(240, 320)
                picture.set_accessible_role(Gtk.AccessibleRole.IMG)
                picture.update_property([Gtk.AccessibleProperty.LABEL], [art_label])
                art_pictures.append((picture, art_name, (720, 960)))
                render_illustration(picture, art_name)
                art_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                art_card.add_css_class("card")
                art_card.set_halign(Gtk.Align.CENTER)
                art_card.set_valign(Gtk.Align.CENTER)
                art_card.append(picture)
                layout.append(art_card)

            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            copy.set_hexpand(True)
            copy.set_valign(Gtk.Align.CENTER)
            copy_title = Gtk.Label(label=heading, xalign=0, wrap=True)
            copy_title.add_css_class("title-2")
            copy.append(copy_title)
            copy_description = Gtk.Label(label=description, xalign=0, wrap=True)
            copy_description.add_css_class("dimmed")
            copy.append(copy_description)
            if extra:
                copy.append(extra)
            layout.append(copy)
            return layout

        power_note = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        power_note.add_css_class("card")
        power_note.set_margin_top(4)
        power_note.set_margin_bottom(4)
        power_note.set_margin_start(1)
        power_note.set_margin_end(1)
        power_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        power_icon.set_valign(Gtk.Align.START)
        power_note.append(power_icon)
        power_note_text = Gtk.Label(
            label="A dark screen is not always powered off. Disconnect the cable first; if the phone is frozen, hold Power until it shuts down.",
            xalign=0, wrap=True,
        )
        power_note_text.set_margin_top(12)
        power_note_text.set_margin_bottom(12)
        power_note_text.set_margin_end(12)
        power_note.append(power_note_text)
        slides.add_named(slide(
            "connect-power-off.svg",
            "Phone illustration for the first step: power off the phone.",
            "1. Power off the phone",
            "Begin with the device fully shut down. This lets the computer catch the early boot connection instead of an already-running Android system.",
            power_note,
        ), "power")

        mode_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings = Gtk.Settings.get_default()
        animations_enabled = bool(
            settings and settings.get_property("gtk-enable-animations")
        )
        for art_name, mode_title, mode_body in (
            ("connect-usb-buttons.svg", "BootROM mode", "Press and hold Volume Up, Volume Down, or the button combination for your model. Keep holding while you plug in USB. Some devices need both volume buttons or all hardware buttons."),
            ("connect-usb-preloader.svg", "Preloader mode", "With the phone off, plug in USB without holding any hardware buttons."),
        ):
            mode_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            mode_card.add_css_class("card")
            mode_card.set_margin_start(2)
            mode_card.set_margin_end(2)
            mode_picture = Gtk.Picture()
            mode_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            mode_picture.set_can_shrink(True)
            mode_picture.set_size_request(96, 128)
            mode_picture.set_accessible_role(Gtk.AccessibleRole.IMG)
            mode_picture.set_cursor_from_name("zoom-in")
            mode_picture.set_tooltip_text("Hover to enlarge")
            mode_art_label = (
                "BootROM mode: volume buttons highlighted; USB cable muted"
                if mode_title == "BootROM mode"
                else "Preloader mode: USB cable highlighted; hardware buttons muted"
            )
            mode_picture.update_property(
                [Gtk.AccessibleProperty.LABEL], [mode_art_label]
            )
            art_pictures.append((mode_picture, art_name, (720, 960)))
            mode_pictures.append((mode_picture, art_name, (720, 960)))
            render_illustration(mode_picture, art_name)
            mode_card.append(mode_picture)

            preview_picture = Gtk.Picture()
            preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            preview_picture.set_can_shrink(True)
            preview_picture.set_size_request(240, 320)
            preview_picture.set_accessible_role(Gtk.AccessibleRole.IMG)
            preview_picture.update_property(
                [Gtk.AccessibleProperty.LABEL], [mode_art_label]
            )
            art_pictures.append((preview_picture, art_name, (240, 320)))
            mode_pictures.append((preview_picture, art_name, (240, 320)))
            render_illustration(preview_picture, art_name, raster_size=(240, 320))

            preview_popover = Gtk.Popover()
            preview_popover.set_has_arrow(False)
            preview_popover.set_autohide(False)
            preview_popover.set_position(Gtk.PositionType.RIGHT)
            preview_popover.set_child(preview_picture)
            preview_popover.set_parent(mode_picture)
            preview_popovers.append(preview_popover)

            pending_close = [None]

            def cancel_preview_close(*_args, pending_close=pending_close):
                if pending_close[0]:
                    GLib.source_remove(pending_close[0])
                    pending_close[0] = None

            def show_preview(
                *_args, cancel_close=cancel_preview_close,
                preview_popover=preview_popover,
            ):
                cancel_close()
                preview_popover.popup()

            def schedule_preview_close(
                *_args, pending_close=pending_close,
                preview_popover=preview_popover,
                cancel_close=cancel_preview_close,
            ):
                cancel_close()

                def close_preview():
                    pending_close[0] = None
                    preview_popover.popdown()
                    return GLib.SOURCE_REMOVE

                pending_close[0] = GLib.timeout_add(180, close_preview)

            for hover_target in (mode_picture, preview_popover):
                motion = Gtk.EventControllerMotion()
                motion.connect(
                    "enter",
                    show_preview if hover_target is mode_picture else cancel_preview_close,
                )
                motion.connect("leave", schedule_preview_close)
                hover_target.add_controller(motion)
            hover_close_sources.append(pending_close)

            mode_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            mode_copy.set_margin_top(12)
            mode_copy.set_margin_bottom(12)
            mode_copy.set_margin_end(12)
            mode_heading = Gtk.Label(label=mode_title, xalign=0)
            mode_heading.add_css_class("heading")
            mode_copy.append(mode_heading)
            mode_detail = Gtk.Label(label=mode_body, xalign=0, wrap=True)
            mode_detail.add_css_class("dimmed")
            mode_copy.append(mode_detail)
            mode_card.append(mode_copy)
            mode_cards.append(mode_card)
        slides.add_named(slide(
            None,
            "",
            "2. Choose a connection mode",
            "The right method depends on the phone. Try the mode requested by your device or its service instructions.",
            mode_cards,
        ), "mode")
        if animations_enabled:
            self._connection_guide_pulse_source = GLib.timeout_add(
                900, pulse_mode_actions
            )
        self._connection_guide_hover_sources = hover_close_sources
        self._connection_guide_popovers = preview_popovers

        testpoint_note = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        testpoint_note.add_css_class("card")
        warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        warning_icon.set_valign(Gtk.Align.START)
        warning_icon.set_margin_start(12)
        warning_icon.set_margin_top(14)
        testpoint_note.append(warning_icon)
        warning_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        warning_copy.set_margin_top(12)
        warning_copy.set_margin_bottom(12)
        warning_copy.set_margin_end(12)
        warning_title = Gtk.Label(label="Use the exact board diagram", xalign=0)
        warning_title.add_css_class("heading")
        warning_copy.append(warning_title)
        warning_body = Gtk.Label(
            label="Test-point locations differ by model. Short only the documented point to ground while connecting USB. The wrong pads can damage the device; do not guess.",
            xalign=0, wrap=True,
        )
        warning_body.add_css_class("dimmed")
        warning_copy.append(warning_body)
        testpoint_note.append(warning_copy)
        slides.add_named(slide(
            "connect-test-point.svg",
            "Close-up illustration of a circuit-board test point marked TP1.",
            "3. If it still isn’t detected",
            "Some devices need a hardware test point to enter BootROM. The original guide marks an example point as TP1; that label and location are not universal.",
            testpoint_note,
        ), "testpoint")
        page.append(slides)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        back_button = Gtk.Button(label="Back")
        next_button = Gtk.Button(label="Next")
        next_button.add_css_class("suggested-action")
        actions.append(back_button)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)
        close_button = Gtk.Button(label="Close guide")
        actions.append(close_button)
        actions.append(next_button)
        page.append(actions)

        slide_names = ("power", "mode", "testpoint")
        current_step = 0

        def show_step(index):
            nonlocal current_step
            current_step = max(0, min(index, len(slide_names) - 1))
            if current_step != 1:
                for popover in preview_popovers:
                    popover.popdown()
            slides.set_visible_child_name(slide_names[current_step])
            step_count.set_text(f"STEP {current_step + 1} OF {len(slide_names)}")
            for segment_index, segment in enumerate(progress_bars):
                segment.set_fraction(1 if segment_index <= current_step else 0)
                segment.update_property(
                    [Gtk.AccessibleProperty.LABEL],
                    [f"Step {segment_index + 1} of {len(slide_names)}"],
                )
            back_button.set_sensitive(current_step > 0)
            next_button.set_label(
                "Start device detection" if current_step == len(slide_names) - 1 else "Next"
            )

        def advance(*_args):
            if current_step == len(slide_names) - 1:
                window.close()
                self.connect_backend()
            else:
                show_step(current_step + 1)

        back_button.connect("clicked", lambda *_: show_step(current_step - 1))
        next_button.connect("clicked", advance)
        close_button.connect("clicked", lambda *_: window.close())
        show_step(0)

        clamp = Adw.Clamp(maximum_size=860)
        clamp.set_child(page)
        toolbar.set_content(clamp)
        window.set_content(toolbar)
        window.connect("close-request", self._connection_guide_closed)
        self.connection_guide = window
        window.present()

    def _connection_guide_closed(self, *_args):
        if getattr(self, "_connection_guide_pulse_source", None):
            GLib.source_remove(self._connection_guide_pulse_source)
            self._connection_guide_pulse_source = None
        for source in getattr(self, "_connection_guide_hover_sources", []):
            if source[0]:
                GLib.source_remove(source[0])
                source[0] = None
        self._connection_guide_hover_sources = []
        for popover in getattr(self, "_connection_guide_popovers", []):
            if popover.get_parent():
                popover.popdown()
                popover.unparent()
        self._connection_guide_popovers = []
        if getattr(self, "_connection_guide_theme_handler", None):
            Adw.StyleManager.get_default().disconnect(
                self._connection_guide_theme_handler
            )
            self._connection_guide_theme_handler = None
        self.connection_guide = None
        return GLib.SOURCE_REMOVE

    def open_preferences(self, *_args):
        if getattr(self, "preferences_window", None):
            self.preferences_window.present()
            return

        window = Adw.PreferencesWindow(transient_for=self, modal=True, title="Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        group = Adw.PreferencesGroup(
            title="MTK Client backend",
            description="Choose the source checkout used to run device operations.",
        )
        row = Adw.ActionRow(title="Checkout location", subtitle=MTKCLIENT_DIR)
        choose_button = Gtk.Button(label="Choose…")
        choose_button.set_valign(Gtk.Align.CENTER)
        choose_button.connect("clicked", self._choose_backend_dir)
        row.add_suffix(choose_button)
        group.add(row)
        page.add(group)
        logging_group = Adw.PreferencesGroup(
            title="Diagnostics",
            description="Advanced logging includes low-level USB packet details and may be verbose. Changes apply after restarting the app.",
        )
        self.advanced_logging_switch = Gtk.Switch(
            active=get_advanced_logging(), valign=Gtk.Align.CENTER
        )
        self.advanced_logging_switch.connect("notify::active", self._advanced_logging_changed)
        logging_row = Adw.ActionRow(
            title="Advanced logging",
            subtitle="Include detailed backend debug messages in Logs.",
        )
        logging_row.add_suffix(self.advanced_logging_switch)
        logging_row.set_activatable_widget(self.advanced_logging_switch)
        logging_group.add(logging_row)
        page.add(logging_group)
        window.add(page)
        window.connect("close-request", self._preferences_closed)
        self.preferences_window = window
        self.backend_path_row = row
        window.present()

    def _advanced_logging_changed(self, switch, _param):
        try:
            set_advanced_logging(switch.get_active())
        except OSError as error:
            switch.set_active(not switch.get_active())
            self.toast_overlay.add_toast(Adw.Toast(title=f"Could not save logging preference: {error}"))

    def _preferences_closed(self, *_args):
        self.preferences_window = None
        return GLib.SOURCE_REMOVE

    def _choose_backend_dir(self, *_args):
        chooser = Gtk.FileChooserNative.new(
            "Select MTK Client checkout",
            self.preferences_window,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "Select",
            "Cancel",
        )
        chooser.connect("response", self._backend_dir_chosen)
        chooser.show()

    def _backend_dir_chosen(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file()
            path = folder.get_path() if folder else None
            if path:
                path = os.path.realpath(path)
                if not (os.path.isfile(os.path.join(path, "pyproject.toml"))
                        and os.path.isfile(os.path.join(path, "uv.lock"))):
                    self.toast_overlay.add_toast(Adw.Toast(
                        title="Choose a checkout containing pyproject.toml and uv.lock"
                    ))
                else:
                    set_backend_dir(path)
                    self.backend_path_row.set_subtitle(path)
                    self.toast_overlay.add_toast(Adw.Toast(
                        title="Backend location saved. Restart the app to use it."
                    ))
        chooser.destroy()

    @staticmethod
    def _scrolled(child):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        return scroller

    def _connection_page(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        self.connection_icon = Gtk.Image.new_from_icon_name("phone-symbolic")
        self.connection_icon.set_pixel_size(48)
        self.connection_icon.add_css_class("dimmed")
        content.append(self.connection_icon)

        title = Gtk.Label(label="Connect a MediaTek device")
        title.add_css_class("title-1")
        content.append(title)

        self.connection_status = Gtk.Label(
            label="Connect a powered-off device to begin. The connection method depends on your device."
        )
        self.connection_status.set_wrap(True)
        self.connection_status.set_justify(Gtk.Justification.CENTER)
        self.connection_status.set_max_width_chars(56)
        self.connection_status.add_css_class("dimmed")
        content.append(self.connection_status)

        self.connection_help_button = Gtk.Button(label="Connection guide")
        self.connection_help_button.add_css_class("flat")
        self.connection_help_button.set_halign(Gtk.Align.CENTER)
        self.connection_help_button.connect("clicked", self.open_connection_guide)
        content.append(self.connection_help_button)

        self.connection_options_button = self._connection_options_menu()

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(48, 48)
        self.spinner.add_css_class("connecting-spinner")
        self.spinner.set_visible(False)
        content.append(self.spinner)

        self.connect_button = Gtk.Button(label="Start device detection")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.set_halign(Gtk.Align.CENTER)
        self.connect_button.connect("clicked", lambda *_: self.connect_backend())
        content.append(self.connect_button)

        self.device_details = Gtk.Label()
        self.device_details.add_css_class("dimmed")
        self.device_details.set_visible(False)
        content.append(self.device_details)
        clamp = Adw.Clamp(maximum_size=680)
        clamp.set_child(content)
        return self._scrolled(clamp)

    def _connection_options_menu(self):
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_tooltip_text("Connection options")
        button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Connection options"]
        )

        popover = Gtk.Popover()
        options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        options.set_size_request(320, -1)
        options.set_margin_top(16)
        options.set_margin_bottom(16)
        options.set_margin_start(16)
        options.set_margin_end(16)

        heading = Gtk.Label(label="Connection options", xalign=0)
        heading.add_css_class("heading")
        options.append(heading)
        hint = Gtk.Label(label="Optional settings for specific devices.", xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dimmed")
        options.append(hint)

        self.iot_switch = Gtk.CheckButton(label="Use IoT mode")
        self.iot_switch.set_tooltip_text("Enable for supported MediaTek IoT devices")
        options.append(self.iot_switch)

        self.serial_entry = Gtk.Entry()
        self.serial_entry.set_placeholder_text("Serial port (optional)")
        self.serial_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Serial port (optional)"]
        )
        options.append(self.serial_entry)

        for title, file_kind in (("Download Agent loader", "loader"),
                                 ("Preloader file", "preloader")):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            copy.set_hexpand(True)
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("heading")
            status = Gtk.Label(label="Not selected", xalign=0)
            status.add_css_class("caption")
            status.add_css_class("dimmed")
            status.set_ellipsize(Pango.EllipsizeMode.END)
            copy.append(label)
            copy.append(status)
            choose = Gtk.Button(label="Choose…")
            choose.connect("clicked", lambda *_args, kind=file_kind:
                           self._choose_connection_file(kind))
            row.append(copy)
            row.append(choose)
            options.append(row)
            if file_kind == "loader":
                self.loader_row = status
                self.loader_button = choose
            else:
                self.preloader_row = status
                self.preloader_button = choose

        popover.set_child(options)
        button.set_popover(popover)
        return button

    def _update_connection_options_visibility(self, *_args):
        self.connection_options_button.set_visible(
            self.stack.get_visible_child_name() == "device"
        )

    @staticmethod
    def _page_column(maximum_size=920):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        clamp = Adw.Clamp(maximum_size=maximum_size)
        clamp.set_child(content)
        return content, clamp

    @staticmethod
    def _page_heading(title, subtitle):
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("title-1")
        description = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        description.add_css_class("dimmed")
        header.append(title_label)
        header.append(description)
        return header

    def _partition_page(self):
        return self._partitions_content()

    def _partitions_content(self):
        box, clamp = self._page_column()
        box.append(self._page_heading(
            "Flash partitions", "Choose one or more GPT partitions, then select an action."
        ))
        self.operation_status = Gtk.Label(label="Connect a device to load its partition map.", xalign=0)
        self.operation_status.add_css_class("dimmed")
        box.append(self.operation_status)
        self.operation_progress = Gtk.ProgressBar()
        self.operation_progress.set_show_text(True)
        self.operation_progress.set_visible(False)
        box.append(self.operation_progress)
        partition_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.partition_search = Gtk.SearchEntry()
        self.partition_search.set_placeholder_text("Search partitions")
        self.partition_search.set_accessible_role(Gtk.AccessibleRole.SEARCH_BOX)
        self.partition_search.set_hexpand(True)
        self.partition_search.connect("search-changed", lambda *_: self._refresh_partition_view())
        partition_controls.append(self.partition_search)
        self.partition_sort = Gtk.DropDown.new_from_strings(
            ["Chip layout", "Alphabetical"]
        )
        self.partition_sort.set_tooltip_text("Sort partitions")
        self.partition_sort.connect("notify::selected", lambda *_: self._refresh_partition_view())
        partition_controls.append(self.partition_sort)
        box.append(partition_controls)

        self.select_all = Gtk.CheckButton(label="Select visible partitions")
        self.select_all.connect("toggled", self._toggle_all_partitions)
        self.select_all.set_sensitive(False)
        self.dump_gpt = Gtk.CheckButton(label="Include GPT metadata when reading")
        self.dump_gpt.set_active(True)
        box.append(self.select_all)

        self.partition_list = Gtk.ListBox()
        self.partition_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.partition_list.add_css_class("boxed-list")
        self.partition_stack = Gtk.Stack()
        self.partition_stack.add_named(self.partition_list, "partitions")
        self.partition_empty = Gtk.Label(label="Connect a device to load its partition map.")
        self.partition_empty.add_css_class("dimmed")
        self.partition_empty.set_wrap(True)
        self.partition_stack.add_named(self.partition_empty, "empty")
        # Let the outer page scroller handle vertical scrolling so the boxed
        # list's first and last rows (and their rounded corners) stay together.
        box.append(self.partition_stack)

        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_top(4)
        action_bar.set_margin_bottom(4)
        self.selection_count = Gtk.Label(label="0 selected")
        self.selection_count.set_xalign(0)
        action_bar.append(self.selection_count)
        action_bar.append(Gtk.Box())
        action_bar.get_last_child().set_hexpand(True)
        self.read_button = Gtk.Button(label="Read…")
        self.read_button.connect("clicked", lambda *_: self._choose_directory("read"))
        self.write_button = Gtk.Button(label="Write…")
        self.write_button.connect("clicked", lambda *_: self._choose_directory("write"))
        self.erase_button = Gtk.Button(label="Erase…")
        self.erase_button.add_css_class("destructive-action")
        self.erase_button.connect("clicked", lambda *_: self._confirm_erase())
        for button in (self.read_button, self.write_button, self.erase_button):
            button.set_sensitive(False)
        action_bar.append(self.read_button)
        action_bar.append(self.write_button)
        action_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        action_bar.append(self.erase_button)
        box.append(self.dump_gpt)
        box.append(action_bar)
        box.append(self._storage_page())
        return self._scrolled(clamp)

    def _storage_page(self):
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        section.set_margin_top(24)
        section.append(self._page_heading(
            "Chip storage", "Read or write a complete eMMC/UFS area. Writes replace the selected area."
        ))
        self.storage_status = Gtk.Label(label="Connect a device to use storage actions.", xalign=0)
        self.storage_status.add_css_class("dimmed")
        section.append(self.storage_status)
        self.storage_progress = Gtk.ProgressBar()
        self.storage_progress.set_show_text(True)
        self.storage_progress.set_visible(False)
        section.append(self.storage_progress)
        raw_group = Adw.PreferencesGroup(title="Storage area")
        raw_group.set_description("Select a chip area, then choose whether to read or replace it.")
        self.raw_target = Gtk.DropDown.new_from_strings(["User storage", "Boot 1", "Boot 2", "RPMB"])
        self.raw_target.set_valign(Gtk.Align.CENTER)
        raw_row = Adw.ActionRow(title="Area")
        raw_row.add_suffix(self.raw_target)
        raw_group.add(raw_row)
        self.raw_read_button = Gtk.Button(label="Read…")
        self.raw_read_button.connect("clicked", lambda *_: self._choose_raw_file("read"))
        self.raw_write_button = Gtk.Button(label="Write…")
        self.raw_write_button.connect("clicked", lambda *_: self._choose_raw_file("write"))
        for button in (self.raw_read_button, self.raw_write_button):
            button.set_sensitive(False)
        action_row = Adw.ActionRow(title="Image actions", subtitle="Writing overwrites the selected area")
        action_row.add_suffix(self.raw_read_button)
        action_row.add_suffix(self.raw_write_button)
        raw_group.add(action_row)
        section.append(raw_group)
        return section

    def _tools_page(self):
        box, clamp = self._page_column(760)
        box.append(self._page_heading(
            "Device tools", "Security-sensitive utilities. Review the device and confirmation carefully."
        ))
        self.tools_status = Gtk.Label(label="Connect a device to use these tools.", xalign=0)
        self.tools_status.add_css_class("dimmed")
        box.append(self.tools_status)
        self.tools_progress = Gtk.ProgressBar()
        self.tools_progress.set_show_text(True)
        self.tools_progress.set_visible(False)
        box.append(self.tools_progress)
        security_group = Adw.PreferencesGroup(title="Security tools")
        security_group.set_description("Keys are saved locally. Lock state changes may affect device data.")
        self.key_button = Gtk.Button(label="Generate keys…")
        self.key_button.set_sensitive(False)
        self.key_button.connect("clicked", lambda *_: self._choose_key_directory())
        key_row = Adw.ActionRow(title="Device keys",
                                subtitle="Generate and save keys to a selected folder.")
        key_row.add_suffix(self.key_button)
        security_group.add(key_row)
        security_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        security_buttons.set_halign(Gtk.Align.END)
        self.unlock_button = Gtk.Button(label="Unlock bootloader…")
        self.unlock_button.set_sensitive(False)
        self.unlock_button.connect("clicked", lambda *_: self._confirm_lock_action("unlock"))
        self.lock_button = Gtk.Button(label="Lock bootloader…")
        self.lock_button.set_sensitive(False)
        self.lock_button.connect("clicked", lambda *_: self._confirm_lock_action("lock"))
        for button in (self.unlock_button, self.lock_button):
            security_buttons.append(button)
        security_group.add(security_buttons)
        box.append(security_group)
        return self._scrolled(clamp)

    def _utilities_page(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.utility_stack = Adw.ViewStack()
        self.utility_stack.set_vhomogeneous(False)
        self.utility_stack.add_titled_with_icon(self._tools_page(), "security", "Security",
                                                "changes-prevent-symbolic")
        self.utility_stack.add_titled_with_icon(self._logs_page(), "logs", "Logs",
                                                "text-x-generic-symbolic")
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.utility_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        content.append(switcher)
        self.utility_stack.set_vexpand(True)
        content.append(self.utility_stack)
        return content

    def _logs_page(self):
        view = Gtk.TextView(buffer=self.log_buffer)
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_accessible_role(Gtk.AccessibleRole.LOG)
        view.set_margin_top(12)
        view.set_margin_bottom(12)
        view.set_margin_start(12)
        view.set_margin_end(12)
        return self._scrolled(view)

    def append_log(self, message):
        message = str(message).replace("\x00", "")
        if len(message) > 8_000:
            message = message[:8_000] + " … [message shortened]"
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, f"{time.strftime('%H:%M:%S')}  {message}\n")
        excess = self.log_buffer.get_char_count() - MAX_LOG_CHARS
        if excess > 0:
            self.log_buffer.delete(
                self.log_buffer.get_start_iter(), self.log_buffer.get_iter_at_offset(excess)
            )
        return GLib.SOURCE_REMOVE

    def set_status(self, message):
        self.connection_status.set_text(message)
        return GLib.SOURCE_REMOVE

    def _prepare_backend(self):
        # Import only after GTK has started; backend dependency/setup errors then
        # become visible in both the UI and the launching terminal.
        from mtkclient.Library.DA.mtk_da_handler import DaHandler
        from mtkclient.Library.Partitions.gpt import GptSettings
        from mtkclient.Library.mtk_class import Mtk
        from mtkclient.config.mtk_config import MtkConfig

        # MTK's config writes its log relative to the backend checkout's cwd.
        os.makedirs(os.path.join(MTKCLIENT_DIR, "logs"), exist_ok=True)
        bridge = BackendLog(self)
        loglevel = logging.DEBUG if get_advanced_logging() else logging.INFO
        config = MtkConfig(loglevel=loglevel, gui=bridge,
                           guiprogress=BackendProgress(self))
        config.gpt_settings = GptSettings(gpt_num_part_entries="0",
                                          gpt_part_entry_size="0",
                                          gpt_part_entry_start_lba="0")
        config.reconnect = True
        config.uartloglevel = 2
        config.logchannel = "UART"
        config.write_preloader_to_file = False
        config.iot = self.iot_switch.get_active()
        config.loader = self.loader_path
        if self.preloader_path:
            config.preloader_filename = self.preloader_path
            with open(self.preloader_path, "rb") as stream:
                config.preloader = stream.read()
        serial_port = self.serial_entry.get_text().strip() or None
        mtk = Mtk(config=config, loglevel=loglevel, serialportname=serial_port)
        handler = DaHandler(mtk, loglevel)

        # Mirror backend records into Logs without flooding the launching terminal.
        ui_handler = UiLogHandler(self)
        ui_handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
        logging.getLogger().addHandler(ui_handler)
        # Backend logs belong in the app's Logs view, not an unbounded terminal stream.
        for logger_handler in logging.getLogger().handlers:
            if isinstance(logger_handler, logging.StreamHandler):
                logger_handler.setLevel(logging.CRITICAL + 1)
        return handler

    def connect_backend(self):
        if self.connection_thread and self.connection_thread.is_alive():
            return
        self.connect_button.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.connection_icon.set_from_icon_name("phone-symbolic")
        self.device_details.set_visible(False)
        self.set_status("Initializing MTK backend…")
        self.append_log("Starting MediaTek device detection.")
        self.connection_thread = threading.Thread(target=self._connect_worker,
                                                 name="mtk-connect", daemon=True)
        self.connection_thread.start()

    def _connect_worker(self):
        try:
            os.chdir(MTKCLIENT_DIR)
            if self.backend is None:
                self.backend = self._prepare_backend()
            mtk = self.backend.mtk
            mtk.config.iot = self.iot_switch.get_active()

            GLib.idle_add(self.set_status,
                          "Waiting for the powered-off device. Hold a volume key for bootrom, "
                          "or connect without buttons for preloader mode.")
            print("[mtkclient] Waiting for MediaTek USB device…", flush=True)

            # Follow the established Qt GUI connection sequence so the
            # preloader handshake is listening before the device is claimed.
            if not mtk.port.wait_for_device(max_attempts=1, wait=False):
                mtk.preloader.init()
            else:
                print("[mtkclient] USB device enumerated; performing handshake.", flush=True)

            connected = self.backend.connect(mtk)
            if connected is None:
                raise RuntimeError("Backend could not initialize the device. See the log for details.")
            connected = self.backend.configure_da(connected)
            if connected is None:
                raise RuntimeError("Download Agent setup failed. See the log for details.")

            _gpt_data, gpt = connected.daloader.get_gpt()
            if gpt is None:
                raise RuntimeError("Device connected, but its GPT partition table could not be read.")
            partitions = [
                (entry.name, entry.sectors * gpt.sectorsize)
                for entry in gpt.partentries
            ]

            mode = ("Bootrom mode" if connected.config.is_brom else
                    "DA mode" if connected.config.chipconfig.damode else "Preloader mode")
            chipset = f"{connected.config.chipconfig.name} ({connected.config.chipconfig.description})"
            GLib.idle_add(self._connected, chipset, mode, partitions)
            print(f"[mtkclient] Connected: {chipset} — {mode}", flush=True)
        except Exception as error:
            logging.exception("MTK device connection failed")
            print(f"[mtkclient] Connection failed: {error}", file=sys.stderr, flush=True)
            GLib.idle_add(self._connection_failed, str(error))

    def _connected(self, chipset, mode, partitions):
        self.device_connected = True
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.connect_button.set_sensitive(True)
        self.connect_button.set_label("Reconnect")
        self.connection_icon.set_from_icon_name("phone-symbolic")
        self.connection_status.set_text("Device initialized successfully")
        self.device_details.set_text(f"{chipset}\n{mode}")
        self.device_details.set_visible(True)
        self.partitions = partitions
        self._populate_partitions()
        self._selection_changed()
        self.operation_status.set_text(f"{len(partitions)} partitions available")
        self.storage_status.set_text("Device connected. Select a storage area.")
        self.tools_status.set_text("Device connected. Security tools are available.")
        for button in (self.raw_read_button, self.raw_write_button, self.key_button,
                       self.unlock_button, self.lock_button):
            button.set_sensitive(True)
        self.select_all.set_sensitive(bool(partitions))
        return GLib.SOURCE_REMOVE

    def _populate_partitions(self):
        child = self.partition_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.partition_list.remove(child)
            child = following
        self.partition_checks.clear()
        self.partition_rows.clear()
        self.partition_images.clear()
        self.partition_image_status = {}
        for name, size in self.partitions:
            row = Adw.ActionRow(
                title=name,
                subtitle=f"{self._format_size(size)} · Drop an image or choose one"
            )
            image_status = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            image_status.set_tooltip_text("An image is assigned to this partition")
            image_status.update_property([Gtk.AccessibleProperty.LABEL], ["Image assigned"])
            image_status.set_visible(False)
            row.add_suffix(image_status)
            self.partition_image_status[name] = image_status
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            check.update_property([Gtk.AccessibleProperty.LABEL], [f"Select {name}"])
            row.add_prefix(check)
            check.connect("toggled", lambda *_: self._selection_changed())
            choose = Gtk.Button(icon_name="document-open-symbolic")
            choose.set_valign(Gtk.Align.CENTER)
            choose.set_tooltip_text(f"Choose image file for {name}")
            choose.update_property([Gtk.AccessibleProperty.LABEL], [f"Choose image for {name}"])
            choose.connect("clicked", lambda _button, partition=name:
                           self._choose_partition_image(partition))
            row.add_suffix(choose)
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self._partition_file_dropped, name)
            row.add_controller(drop)
            row.set_tooltip_text("Drop an image file here, or use Choose…")
            self.partition_checks[name] = (check, size)
            self.partition_rows[name] = row
        self._refresh_partition_view()

    def _refresh_partition_view(self):
        if not hasattr(self, "partition_rows"):
            return
        query = self.partition_search.get_text().strip().casefold()
        if self.partition_sort.get_selected() == 1:
            names = sorted(self.partition_rows, key=str.casefold)
        else:
            names = [name for name, _size in self.partitions if name in self.partition_rows]

        child = self.partition_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.partition_list.remove(child)
            child = following

        visible_count = 0
        for name in names:
            row = self.partition_rows[name]
            matches = not query or query in name.casefold()
            row.set_visible(matches)
            if matches:
                visible_count += 1
            self.partition_list.append(row)

        if visible_count:
            self.partition_stack.set_visible_child_name("partitions")
        else:
            self.partition_empty.set_text(
                "No partitions match your search." if query else
                "Connect a device to load its partition map."
            )
            self.partition_stack.set_visible_child_name("empty")
        self.select_all.set_sensitive(self.device_connected and visible_count > 0)

    @staticmethod
    def _format_size(size):
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024

    def _selected_partitions(self):
        return [(name, size) for name, (check, size) in self.partition_checks.items()
                if check.get_active()]

    def _toggle_all_partitions(self, check):
        for name, (partition_check, _size) in self.partition_checks.items():
            if self.partition_rows[name].get_visible():
                partition_check.set_active(check.get_active())

    def _selection_changed(self):
        count = len(self._selected_partitions())
        self.selection_count.set_text(f"{count} selected")
        enabled = self.device_connected and count > 0 and not self.operation_busy
        self.read_button.set_sensitive(enabled)
        self.write_button.set_sensitive(enabled)
        self.erase_button.set_sensitive(enabled)

    def _choose_directory(self, operation):
        selected = self._selected_partitions()
        if not selected:
            self.toast_overlay.add_toast(Adw.Toast(title="Select at least one partition"))
            return
        if operation == "write" and all(name in self.partition_images for name, _ in selected):
            self._start_partition_write(selected, {
                name: self.partition_images[name] for name, _ in selected
            })
            return
        dialog = Gtk.FileChooserNative.new(
            "Choose output folder" if operation == "read" else "Choose folder containing images",
            self, Gtk.FileChooserAction.SELECT_FOLDER, "_Select", "_Cancel"
        )
        dialog.connect("response", self._directory_chosen, operation, selected)
        dialog.show()

    def _choose_connection_file(self, file_kind):
        dialog = Gtk.FileChooserNative.new(
            "Choose Download Agent loader" if file_kind == "loader" else "Choose preloader",
            self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel"
        )
        dialog.connect("response", self._connection_file_chosen, file_kind)
        dialog.show()

    def _choose_partition_image(self, name):
        dialog = Gtk.FileChooserNative.new(
            f"Choose image for {name}", self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel"
        )
        dialog.connect("response", self._partition_image_chosen, name)
        dialog.show()

    def _partition_image_chosen(self, dialog, response, name):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file and file.get_path():
                self._set_partition_image(name, file.get_path())
        dialog.destroy()

    def _partition_file_dropped(self, _target, value, _x, _y, name):
        files = value.get_files() if isinstance(value, Gdk.FileList) else []
        if len(files) == 1:
            path = files[0].get_path()
            if path and os.path.isfile(path):
                self._set_partition_image(name, path)
                return True
        self.toast_overlay.add_toast(Adw.Toast(title="Drop a local image file"))
        return False

    def _set_partition_image(self, name, path):
        self.partition_images[name] = path
        self.partition_image_status[name].set_visible(True)
        self.partition_rows[name].set_subtitle(
            f"{self._format_size(self.partition_checks[name][1])} · {os.path.basename(path)}"
        )
        self.toast_overlay.add_toast(Adw.Toast(title=f"Image assigned to {name}"))

    def _start_partition_write(self, selected, images):
        names = ", ".join(name for name, _ in selected)
        self._confirm_action(
            "Write selected partitions?",
            f"This will overwrite {len(selected)} device partition(s): {names}.",
            "Write", lambda: self._run_operation(
                "Write partitions", self._write_partitions, selected, images,
                total=sum(os.path.getsize(images[name]) for name, _ in selected)
            ), destructive=True
        )

    def _connection_file_chosen(self, dialog, response, file_kind):
        if response == Gtk.ResponseType.ACCEPT:
            path = dialog.get_file().get_path()
            if file_kind == "loader":
                self.loader_path = path
                self.loader_row.set_text(path)
                if self.backend:
                    mtk = self.backend.mtk
                    mtk.config.loader = path
                    mtk.daloader.daconfig.dasetup = {}
                    mtk.daloader.daconfig.parse_da_loader(path, mtk.daloader.daconfig.dasetup)
            else:
                self.preloader_path = path
                self.preloader_row.set_text(path)
                if self.backend:
                    self.backend.mtk.config.preloader_filename = path
                    with open(path, "rb") as stream:
                        self.backend.mtk.config.preloader = stream.read()
        dialog.destroy()

    def _directory_chosen(self, dialog, response, operation, selected):
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file().get_path()
            if operation == "read":
                include_gpt = self.dump_gpt.get_active()
                self._run_operation("Read partitions", self._read_partitions,
                                    selected, folder, include_gpt,
                                    total=selected[0][1] if len(selected) == 1 else None)
            else:
                images = {}
                missing = []
                for name, _size in selected:
                    if name in self.partition_images:
                        images[name] = self.partition_images[name]
                        continue
                    candidates = (f"{name}.bin", f"{name}.img")
                    image = next((os.path.join(folder, filename) for filename in candidates
                                  if os.path.isfile(os.path.join(folder, filename))), None)
                    if image is None:
                        missing.append(name)
                    else:
                        images[name] = image
                if missing:
                    self.toast_overlay.add_toast(Adw.Toast(
                        title=f"No .bin or .img found for: {', '.join(missing[:3])}"
                    ))
                else:
                    self._start_partition_write(selected, images)
        dialog.destroy()

    def _confirm_erase(self):
        selected = self._selected_partitions()
        if not selected:
            self.toast_overlay.add_toast(Adw.Toast(title="Select at least one partition"))
            return
        names = ", ".join(name for name, _ in selected)
        self._confirm_action(
            "Erase selected partitions?",
            f"All data in {len(selected)} partition(s) will be erased: {names}.",
            "Erase", lambda: self._run_operation(
                "Erase partitions", self._erase_partitions, selected,
                total=sum(size for _name, size in selected)
            ), destructive=True
        )

    def _confirm_action(self, heading, body, action, callback, destructive=False):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", action)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        if destructive:
            dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response: callback()
                       if response == "confirm" else None)
        dialog.present(self)

    def _raw_parttype(self):
        return ("user", "boot1", "boot2", "rpmb")[self.raw_target.get_selected()]

    def _choose_raw_file(self, operation):
        parttype = self._raw_parttype()
        chooser_action = (Gtk.FileChooserAction.SAVE if operation == "read"
                          else Gtk.FileChooserAction.OPEN)
        dialog = Gtk.FileChooserNative.new(
            f"Save {parttype} image" if operation == "read" else f"Select image for {parttype}",
            self, chooser_action, "_Select", "_Cancel"
        )
        if operation == "read":
            dialog.set_current_name(f"{parttype}.bin")
        dialog.connect("response", self._raw_file_chosen, operation, parttype)
        dialog.show()

    def _raw_file_chosen(self, dialog, response, operation, parttype):
        if response == Gtk.ResponseType.ACCEPT:
            file_path = dialog.get_file().get_path()
            if operation == "read":
                self._run_operation(
                    f"Read {parttype}", self._read_raw, parttype, file_path,
                    total=self._raw_area_size(parttype)
                )
            else:
                self._confirm_action(
                    f"Write {parttype} image?",
                    f"This will overwrite the {parttype} storage area using {os.path.basename(file_path)}.",
                    "Write", lambda: self._run_operation(
                        f"Write {parttype}", self._write_raw, parttype, file_path,
                        total=os.path.getsize(file_path)
                    ), destructive=True
                )
        dialog.destroy()

    def _read_raw(self, parttype, filename):
        handler = self.backend
        handler.close = lambda: None
        GLib.idle_add(self.append_log, f"Reading raw storage area {parttype} to {filename}")
        if parttype == "rpmb":
            handler.mtk.daloader.read_rpmb(filename)
        else:
            args = SimpleNamespace(filename=filename, parttype=parttype,
                                   offset=None, length=None)
            handler.handle_da_cmds(handler.mtk, "rf", args)

    def _write_raw(self, parttype, filename):
        handler = self.backend
        handler.close = lambda: None
        GLib.idle_add(self.append_log, f"Writing raw storage area {parttype} from {filename}")
        if parttype == "rpmb":
            handler.mtk.daloader.write_rpmb(filename)
        else:
            args = SimpleNamespace(filename=filename, parttype=parttype)
            handler.handle_da_cmds(handler.mtk, "wf", args)

    def _raw_area_size(self, parttype):
        """Return the backend-reported capacity used by a raw-area transfer."""
        try:
            storage = self.backend.mtk.daloader.daconfig.storage
            flashtype = storage.flashtype
            if flashtype == "emmc":
                sizes = {
                    "user": storage.emmc.user_size,
                    "boot1": storage.emmc.boot1_size,
                    "boot2": storage.emmc.boot2_size,
                    "rpmb": storage.emmc.rpmb_size,
                }
                return sizes.get(parttype) or None
            if flashtype == "ufs" and parttype == "user":
                return storage.ufs.lu0_size or storage.flashsize or None
            return storage.flashsize or None
        except (AttributeError, TypeError):
            return None

    def _choose_key_directory(self):
        dialog = Gtk.FileChooserNative.new(
            "Choose folder for device keys", self, Gtk.FileChooserAction.SELECT_FOLDER,
            "_Select", "_Cancel"
        )
        dialog.connect("response", self._key_directory_chosen)
        dialog.show()

    def _key_directory_chosen(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file().get_path()
            self._run_operation("Generate keys", self._generate_keys, folder)
        dialog.destroy()

    def _generate_keys(self, folder):
        import json
        handler = self.backend
        handler.mtk.config.set_hwparam_path(folder)
        GLib.idle_add(self.append_log, "Generating device keys…")
        result = handler.mtk.daloader.keys()
        if not result:
            raise RuntimeError("The backend did not return any keys.")
        output = os.path.join(folder, "hwparam.json")
        with open(output, "w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
        GLib.idle_add(self.append_log, f"Device keys saved to {output}")

    def _confirm_lock_action(self, action):
        self._confirm_action(
            f"{action.capitalize()} the bootloader?",
            "Changing bootloader lock state may erase device data or affect device security. "
            "Proceed only if you understand the consequences.",
            action.capitalize(),
            lambda: self._run_operation(
                f"{action.capitalize()} bootloader", self._set_lock_state, action
            ), destructive=True
        )

    def _set_lock_state(self, action):
        GLib.idle_add(self.append_log, f"Requesting bootloader {action}…")
        success, message = self.backend.mtk.daloader.seccfg(action)
        GLib.idle_add(self.append_log, str(message))
        if not success:
            raise RuntimeError(str(message))

    def _run_operation(self, title, callback, *args, total=None):
        if self.operation_thread and self.operation_thread.is_alive():
            self.toast_overlay.add_toast(Adw.Toast(title="Another device operation is running"))
            return
        self._set_operation_running(True, title)
        self.operation_started_at = time.monotonic()
        self.operation_total = total
        self.operation_progress_position = 0
        self.operation_has_progress = False

        def run():
            try:
                callback(*args)
                GLib.idle_add(self._operation_finished, title, None)
            except BaseException as error:
                logging.exception("%s failed", title)
                print(f"[mtkclient] {title} failed: {error}", file=sys.stderr, flush=True)
                GLib.idle_add(self._operation_finished, title, str(error))

        self.operation_thread = threading.Thread(target=run, name="mtk-operation", daemon=True)
        self.operation_thread.start()

    def _update_operation_progress(self, position):
        """Render backend byte progress and an ETA using the legacy estimator."""
        try:
            position = max(0, int(position))
        except (TypeError, ValueError):
            return GLib.SOURCE_REMOVE
        self.operation_progress_position = position
        self.operation_has_progress = True
        if self.operation_total and self.operation_total > 0:
            fraction = min(position / self.operation_total, 1.0)
            elapsed = time.monotonic() - self.operation_started_at
            remaining = int(elapsed * (self.operation_total / position - 1)) if position else 0
            text = f"{fraction:.0%} · {self._format_duration(remaining)} remaining"
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.set_fraction(fraction)
                bar.set_text(text)
        else:
            # The backend gives byte positions but not a transfer limit for every
            # command. Keep that real activity visible without inventing a percent.
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.set_text(f"{self._format_size(position)} transferred · estimating time…")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _format_duration(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}h:{minutes:02d}m:{seconds:02d}s"
        if minutes:
            return f"{minutes:02d}m:{seconds:02d}s"
        return f"{seconds:02d}s"

    def _set_operation_running(self, running, title=""):
        self.operation_busy = running
        bars = (self.operation_progress, self.storage_progress, self.tools_progress)
        for bar in bars:
            bar.set_visible(running)
        if running:
            for button in (self.read_button, self.write_button, self.erase_button,
                           self.raw_read_button, self.raw_write_button, self.key_button,
                           self.unlock_button, self.lock_button):
                button.set_sensitive(False)
            for check, _size in self.partition_checks.values():
                check.set_sensitive(False)
            self.select_all.set_sensitive(False)
            for bar in bars:
                bar.pulse()
                bar.set_text("Starting…")
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title}… See Logs for details.")
            GLib.timeout_add(120, self._pulse_progress)
        else:
            for bar in bars:
                bar.set_fraction(0)
                bar.set_text("")
            for button in (self.raw_read_button, self.raw_write_button, self.key_button,
                           self.unlock_button, self.lock_button):
                button.set_sensitive(self.device_connected)
            for check, _size in self.partition_checks.values():
                check.set_sensitive(self.device_connected)
            self.select_all.set_sensitive(self.device_connected and bool(self.partitions))
            self._selection_changed()

    def _pulse_progress(self):
        if not self.operation_thread or not self.operation_thread.is_alive():
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.set_visible(False)
            return GLib.SOURCE_REMOVE
        if not self.operation_has_progress:
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _operation_finished(self, title, error):
        self._set_operation_running(False)
        if error:
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title} failed. See Logs for details.")
            self.toast_overlay.add_toast(Adw.Toast(title=f"Operation failed: {error}"))
        else:
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title} completed")
            self.toast_overlay.add_toast(Adw.Toast(title=f"{title} completed"))
        return GLib.SOURCE_REMOVE

    def _read_partitions(self, selected, folder, include_gpt=True):
        handler = self.backend
        handler.close = lambda: None
        for name, _size in selected:
            filename = os.path.join(folder, f"{name}.bin")
            GLib.idle_add(self.append_log, f"Reading {name} to {filename}")
            args = SimpleNamespace(partitionname=name, filename=filename,
                                   parttype=None, offset=None, length=None)
            handler.handle_da_cmds(handler.mtk, "r", args)
        if include_gpt:
            GLib.idle_add(self.append_log, f"Saving GPT metadata to {folder}")
            handler.handle_da_cmds(
                handler.mtk, "gpt", SimpleNamespace(directory=folder)
            )

    def _write_partitions(self, selected, images):
        handler = self.backend
        handler.close = lambda: None
        for name, _size in selected:
            filename = images[name]
            GLib.idle_add(self.append_log, f"Writing {filename} to {name}")
            args = SimpleNamespace(partitionname=name, filename=filename, parttype="user")
            handler.handle_da_cmds(handler.mtk, "w", args)

    def _erase_partitions(self, selected):
        handler = self.backend
        handler.close = lambda: None
        for name, _size in selected:
            GLib.idle_add(self.append_log, f"Erasing partition {name}")
            args = SimpleNamespace(partitionname=name, parttype="user")
            handler.handle_da_cmds(handler.mtk, "e", args)

    def _connection_failed(self, error):
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.connect_button.set_sensitive(True)
        self.connect_button.set_label("Try again")
        self.connection_status.set_text(
            "Could not initialize the device. Check USB/udev permissions, power it off, "
            "select bootrom or preloader mode, and review Logs."
        )
        self.toast_overlay.add_toast(Adw.Toast(title=f"Connection failed: {error}"))
        return GLib.SOURCE_REMOVE


class MtkApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = MtkWindow(self)
        window.present()


def main():
    Adw.init()
    return MtkApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
