#!/usr/bin/env python3
"""GNOME-native frontend for the existing MTK device connection backend."""

import logging
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

import cairo

from app_settings import (APP_CONFIG_DIR, get_advanced_logging, get_backend_dir,
                          get_firmware_image_directory,
                          get_pinned_partitions, set_advanced_logging,
                          set_backend_dir, set_firmware_image_directory,
                          set_pinned_partitions)
from mtk_image_identify import (ImageIndex, identify as identify_images,
                                make_da_reader)

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
        GLib.idle_add(
            self.window._update_operation_progress,
            self.window.operation_progress_offset + position,
        )


class MtkWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MTK Client")
        self.set_default_size(900, 650)
        self.backend = None
        self.connection_thread = None
        self.connection_status_source = None
        self.connection_search_slow_source = None
        self.connection_status_phrases = ()
        self.connection_status_phrase_index = 0
        self.operation_thread = None
        self.operation_title = None
        self.pending_device_operations = []
        self.identify_index_thread = None
        self.reboot_thread = None
        self.device_connected = False
        self.brom_mode = False
        self.preloader_mode = False
        self.operation_busy = False
        self.operation_started_at = None
        self.operation_total = None
        self.operation_progress_position = 0
        self.operation_progress_offset = 0
        self.operation_has_progress = False
        self.connected_mark_started_at = None
        self.connection_success_timeout = None
        self.loader_path = None
        self.preloader_path = None
        self.partitions = []
        self.partition_geometry = {}
        self.identify_directory = get_firmware_image_directory()
        self.identify_restore_started = False
        self.identify_index = None
        self.identify_monitor = None
        self.identify_monitors = []
        self.identify_monitor_stack = []
        self.identify_monitor_walk_source = None
        self.identify_poll_source = None
        self.identify_debounce = None
        self.identify_pending_change = False
        self.identify_cancel = None
        self.identify_generation = 0
        self.identify_progress_complete = True
        self.identify_has_progress = False
        self.identify_waiting_on_read = False
        self.identify_window = None
        self.partition_checks = {}
        self.partition_rows = {}
        self.partition_source_labels = {}
        self.partition_sources = {}
        self.partition_pin_status = {}
        self.partition_images = {}
        self.pinned_partitions = set(get_pinned_partitions())
        self.log_buffer = Gtk.TextBuffer()

        # One shared context menu pins the partition that was right-clicked.
        self._partition_menu_target = None
        self._partition_menu_popover = Gtk.PopoverMenu()
        self._partition_menu_popover.set_has_arrow(False)
        # Keep the popover anchored after closing; unparenting from "closed"
        # races the menu item's action dispatch and makes the item a no-op.
        menu_actions = Gio.SimpleActionGroup()
        pin_action = Gio.SimpleAction.new("toggle-pin", None)
        pin_action.connect("activate", self._toggle_partition_pin_action)
        menu_actions.add_action(pin_action)
        self._partition_menu_popover.insert_action_group("row", menu_actions)

        self.header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        self.stack.set_vhomogeneous(False)
        self.switcher = Adw.ViewSwitcher()
        self.switcher.set_stack(self.stack)
        self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self.header.set_title_widget(self.switcher)
        self.navigation_button = self._navigation_menu()
        self.reboot_button = Gtk.Button()
        # Ship this icon directly so the desktop icon theme cannot substitute
        # a refresh glyph for the reboot symbol.
        self.reboot_icon = Gtk.Image.new_from_file(
            os.path.join(APP_DIR, "assets", "reboot-device.png")
        )
        self.reboot_icon.set_pixel_size(16)
        self.reboot_button.set_child(self.reboot_icon)
        self.reboot_button.set_tooltip_text("Reboot Device")
        self.reboot_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Reboot Device from BROM or preloader"]
        )
        self.reboot_button.connect("clicked", self._reboot_device)
        self.header.pack_end(self.navigation_button)
        self.header.pack_end(self.reboot_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self.stack)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        # Build connection settings before the user can start backend setup.
        self.connection_options_row = self._connection_options_panel()
        connection_page = self._connection_page()
        self.stack.add_named(connection_page, "device")
        self.stack.add_titled_with_icon(self._partition_page(), "partitions", "Flash",
                                        "drive-harddisk-symbolic")
        self.stack.add_titled_with_icon(self._utilities_page(), "utilities", "Utilities",
                                        "applications-system-symbolic")
        self.stack.add_named(self._logs_page(), "logs")
        self.stack.set_visible_child_name("device")
        self._update_navigation_visibility()

        # Ctrl+F reveals the Flash page and focuses its search field from anywhere
        # in the window, matching the shortcut users expect from searchable lists.
        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.GLOBAL)
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>f"),
            Gtk.CallbackAction.new(self.focus_partition_search),
        ))
        self.add_controller(shortcuts)

    def focus_partition_search(self, *_args):
        self.stack.set_visible_child_name("partitions")
        self.partition_search.grab_focus()
        self.partition_search.select_region(0, -1)
        return True

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
        title = Gtk.Label(label="Let’s get your device connected", xalign=0)
        title.add_css_class("title-1")
        intro.append(title)
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
        preview_pictures = []
        preview_popovers = []
        hover_close_sources = []
        animation_values = {"cable": 1.0, "press": 1.0}
        animation_started_at = None
        illustration_sources = {}
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
            picture, art_name, cable_progress=1.0, press_progress=0.0,
            raster_size=(720, 960),
        ):
            dark = style_manager.get_dark()
            palette = dict(art_palettes[dark])
            if art_name not in illustration_sources:
                path = os.path.join(APP_DIR, "assets", art_name)
                with open(path, "rb") as svg_file:
                    illustration_sources[art_name] = svg_file.read().decode("utf-8")
            svg = illustration_sources[art_name]
            color_map = {
                light_value: palette[name]
                for name, light_value in art_palettes[False].items()
            }
            svg = re.sub(
                r"#[0-9a-fA-F]{6}",
                lambda match: color_map.get(match.group(0), match.group(0)),
                svg,
            )
            if art_name in (
                "connect-usb-buttons.svg", "connect-usb-preloader.svg"
            ):
                cable_progress = max(0.0, min(1.0, cable_progress))
                plug_x = 60 * (1 - cable_progress)
                plug_y = 30 * (1 - cable_progress)
                cable_start_x = 180 + plug_x
                cable_start_y = 430 + plug_y
                cable_control_x = 230 + plug_x * 0.45
                cable_path = (
                    f"M{cable_start_x:.2f} {cable_start_y:.2f} "
                    f"C{cable_start_x:.2f} {cable_start_y + 14:.2f} "
                    f"{cable_control_x:.2f} 464 340 464"
                )
                for cable_id in ("usb-cable", "usb-cable-highlight"):
                    svg = re.sub(
                        rf'(<path id="{cable_id}" d=")[^"]+(" )',
                        lambda match: (
                            f'{match.group(1)}{cable_path}{match.group(2)}'
                        ),
                        svg,
                    )
                svg = re.sub(
                    r'(<g id="usb-plug" transform="translate\()[^"]+(" )',
                    lambda match: (
                        f'{match.group(1)}{plug_x:.2f} {plug_y:.2f}'
                        f'{match.group(2)}'
                    ),
                    svg,
                )
            if art_name == "connect-usb-buttons.svg":
                press_offset = -4 + 4 * max(0.0, min(1.0, press_progress))
                svg = re.sub(
                    r'(<g id="button-cues" transform="translate\()[^"]+(" )',
                    lambda match: (
                        f'{match.group(1)}{press_offset:.2f} 0{match.group(2)}'
                    ),
                    svg,
                )
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.set_size(*raster_size)
            loader.write(svg.encode("utf-8"))
            loader.close()
            picture.set_paintable(Gdk.Texture.new_for_pixbuf(loader.get_pixbuf()))

        def update_illustration_theme(*_args):
            for picture, art_name, raster_size in art_pictures:
                is_mode_picture = any(
                    mode_picture is picture
                    for mode_picture, _, _ in mode_pictures
                )
                render_illustration(
                    picture, art_name,
                    cable_progress=(
                        animation_values["cable"]
                        if is_mode_picture else 1.0
                    ),
                    press_progress=(
                        animation_values["press"]
                        if is_mode_picture else 1.0
                    ),
                    raster_size=raster_size,
                )

        def animate_mode_actions():
            nonlocal animation_started_at
            if slides.get_visible_child_name() != "mode":
                return GLib.SOURCE_CONTINUE
            now = GLib.get_monotonic_time()
            if animation_started_at is None:
                animation_started_at = now
            cycle = ((now - animation_started_at) / 1_000_000) % 3.6

            def ease(progress):
                progress = max(0.0, min(1.0, progress))
                return progress * progress * (3 - 2 * progress)

            if cycle < 0.45:
                cable_progress = 0.0
            elif cycle < 1.15:
                cable_progress = ease((cycle - 0.45) / 0.7)
            elif cycle < 2.15:
                cable_progress = 1.0
            elif cycle < 2.85:
                cable_progress = 1.0 - ease((cycle - 2.15) / 0.7)
            else:
                cable_progress = 0.0

            if cycle < 0.35:
                press_progress = ease(cycle / 0.35)
            elif cycle < 2.55:
                press_progress = 1.0
            elif cycle < 2.95:
                press_progress = 1.0 - ease((cycle - 2.55) / 0.4)
            else:
                press_progress = 0.0

            animation_values["cable"] = cable_progress
            animation_values["press"] = press_progress
            for picture, art_name, raster_size in mode_pictures:
                if picture in preview_pictures:
                    popover = picture.get_parent()
                    if not popover or not popover.get_visible():
                        continue
                render_illustration(
                    picture, art_name,
                    cable_progress=cable_progress,
                    press_progress=press_progress,
                    raster_size=raster_size,
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
            label="A dark screen is not always powered off.\n"
            "Disconnect the cable first. \n"
            "If the phone is frozen, hold Power until it shuts down.",
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
            "Begin with the device fully shut down.",
            power_note,
        ), "power")

        mode_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings = Gtk.Settings.get_default()
        animations_enabled = bool(
            settings and settings.get_property("gtk-enable-animations")
        )
        initial_motion = 0.0 if animations_enabled else 1.0
        animation_values["cable"] = initial_motion
        animation_values["press"] = initial_motion
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
            mode_picture.set_size_request(120, 160)
            mode_picture.set_accessible_role(Gtk.AccessibleRole.IMG)
            mode_picture.set_cursor_from_name("zoom-in")
            mode_picture.set_tooltip_text("Hover to enlarge")
            mode_art_label = (
                "BootROM mode: volume buttons held while the USB plug moves into the phone"
                if mode_title == "BootROM mode"
                else "Preloader mode: USB plug moves into the phone; no buttons are held"
            )
            mode_picture.update_property(
                [Gtk.AccessibleProperty.LABEL], [mode_art_label]
            )
            art_pictures.append((mode_picture, art_name, (360, 480)))
            mode_pictures.append((mode_picture, art_name, (360, 480)))
            render_illustration(
                mode_picture, art_name,
                cable_progress=initial_motion,
                press_progress=initial_motion,
                raster_size=(360, 480),
            )
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
            preview_pictures.append(preview_picture)
            render_illustration(
                preview_picture, art_name,
                cable_progress=initial_motion,
                press_progress=initial_motion,
                raster_size=(240, 320),
            )

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
            label="Test-point locations differ by model. Short only the documented point to ground while connecting USB. The wrong pads can damage the device, so don't try guessing them yourself.",
            xalign=0, wrap=True,
        )
        warning_body.add_css_class("dimmed")
        warning_copy.append(warning_body)
        testpoint_note.append(warning_copy)
        slides.add_named(slide(
            "connect-test-point.svg",
            "Example board with a probe touching TP1 beside a separate GND pad; not a device-specific layout.",
            "3. If it still isn’t detected",
            "Some devices need shorting a hardware test point to enter BootROM.\n"
            "This is more complex, and may require additional tools or expertise.\n"
            "You will probably have to research the specific test point for your device.",
            testpoint_note,
        ), "testpoint")
        page.append(slides)

        actions = Gtk.Overlay()
        arrow_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        arrow_actions.set_halign(Gtk.Align.CENTER)
        center_actions = Gtk.CenterBox()
        center_actions.set_hexpand(True)
        center_actions.set_center_widget(arrow_actions)
        actions.set_child(center_actions)
        actions.set_hexpand(True)
        back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_button.set_tooltip_text("Previous step")
        back_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Previous step"]
        )
        back_button.set_size_request(44, 44)
        next_button = Gtk.Button.new_from_icon_name("go-next-symbolic")
        next_button.set_tooltip_text("Next step")
        next_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Next step"]
        )
        next_button.set_size_request(44, 44)
        arrow_actions.append(back_button)
        arrow_actions.append(next_button)
        done_button = Gtk.Button(label="Done")
        done_button.add_css_class("suggested-action")
        done_button.set_halign(Gtk.Align.END)
        done_button.set_valign(Gtk.Align.CENTER)
        actions.add_overlay(done_button)
        page.append(actions)

        slide_names = ("power", "mode", "testpoint")
        current_step = 0
        progress_sources = [None] * len(progress_bars)
        self._connection_guide_progress_sources = progress_sources

        def animate_progress(segment_index, target):
            source_id = progress_sources[segment_index]
            if source_id:
                GLib.source_remove(source_id)
                progress_sources[segment_index] = None

            segment = progress_bars[segment_index]
            start = segment.get_fraction()
            if not animations_enabled:
                segment.set_fraction(target)
                return

            started_at = GLib.get_monotonic_time()
            duration_us = 300_000

            def update_progress():
                elapsed = GLib.get_monotonic_time() - started_at
                progress = min(1.0, elapsed / duration_us)
                eased = 1 - (1 - progress) ** 3
                segment.set_fraction(start + (target - start) * eased)
                if progress >= 1:
                    progress_sources[segment_index] = None
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            progress_sources[segment_index] = GLib.timeout_add(
                16, update_progress
            )

        def show_step(index):
            nonlocal current_step, animation_started_at
            current_step = max(0, min(index, len(slide_names) - 1))
            if current_step != 1:
                for popover in preview_popovers:
                    popover.popdown()
            slides.set_visible_child_name(slide_names[current_step])
            animation_source = getattr(
                self, "_connection_guide_animation_source", None
            )
            if current_step == 1 and animations_enabled:
                if not animation_source:
                    animation_started_at = GLib.get_monotonic_time()
                    animate_mode_actions()
                    self._connection_guide_animation_source = GLib.timeout_add(
                        33, animate_mode_actions
                    )
            else:
                if animation_source:
                    GLib.source_remove(animation_source)
                    self._connection_guide_animation_source = None
                animation_started_at = None
            step_count.set_text(f"STEP {current_step + 1} OF {len(slide_names)}")
            for segment_index, segment in enumerate(progress_bars):
                animate_progress(
                    segment_index,
                    1 if segment_index <= current_step else 0,
                )
                segment.update_property(
                    [Gtk.AccessibleProperty.LABEL],
                    [f"Step {segment_index + 1} of {len(slide_names)}"],
                )
            back_button.set_sensitive(current_step > 0)
            next_button.set_sensitive(current_step < len(slide_names) - 1)
            next_label = (
                "Start device detection"
                if current_step == len(slide_names) - 1 else "Next step"
            )
            next_button.set_tooltip_text(next_label)
            next_button.update_property(
                [Gtk.AccessibleProperty.LABEL], [next_label]
            )

        def advance(*_args):
            if current_step == len(slide_names) - 1:
                window.close()
                self.connect_backend()
            else:
                show_step(current_step + 1)

        back_button.connect("clicked", lambda *_: show_step(current_step - 1))
        next_button.connect("clicked", advance)
        done_button.connect("clicked", lambda *_: window.close())
        escape = Gtk.ShortcutController()
        escape.set_scope(Gtk.ShortcutScope.LOCAL)
        escape.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_args: (window.close(), True)[1]),
        ))
        window.add_controller(escape)
        show_step(0)

        clamp = Adw.Clamp(maximum_size=860)
        clamp.set_child(page)
        toolbar.set_content(clamp)
        window.set_content(toolbar)
        window.connect("close-request", self._connection_guide_closed)
        self.connection_guide = window
        window.present()

    def _connection_guide_closed(self, *_args):
        if getattr(self, "_connection_guide_animation_source", None):
            GLib.source_remove(self._connection_guide_animation_source)
            self._connection_guide_animation_source = None
        for source_id in getattr(
            self, "_connection_guide_progress_sources", []
        ):
            if source_id:
                GLib.source_remove(source_id)
        self._connection_guide_progress_sources = []
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

        self.connection_illustration = Gtk.Picture.new_for_filename(
            os.path.join(APP_DIR, "assets", "device-ready.svg")
        )
        self.connection_illustration.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.connection_illustration.set_can_shrink(True)
        self.connection_illustration.set_size_request(240, 160)
        self.connection_illustration.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.connection_illustration.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["A powered-off phone and USB cable, ready to connect"],
        )
        content.append(self.connection_illustration)

        self.connection_title = Gtk.Label(label="Connect a MediaTek device")
        self.connection_title.add_css_class("title-1")
        content.append(self.connection_title)

        self.connection_status = Gtk.Label(
            label=""
        )
        self.connection_status.set_wrap(True)
        self.connection_status.set_justify(Gtk.Justification.CENTER)
        self.connection_status.set_max_width_chars(56)
        self.connection_status.set_size_request(-1, 52)
        self.connection_status.set_lines(2)
        self.connection_status.set_ellipsize(Pango.EllipsizeMode.END)
        self.connection_status.set_valign(Gtk.Align.CENTER)
        self.connection_status.add_css_class("dimmed")
        content.append(self.connection_status)

        self.connection_help_button = Gtk.Button.new_from_icon_name(
            "dialog-question-symbolic"
        )
        self.connection_help_button.set_tooltip_text("Connection guide")
        self.connection_help_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Open connection guide"]
        )
        self.connection_help_button.add_css_class("circular")
        self.connection_help_button.set_halign(Gtk.Align.CENTER)
        self.connection_help_button.connect("clicked", self.open_connection_guide)
        content.append(self.connection_help_button)

        # Keep the loading indicator in its own reserved row between help and
        # the primary action; toggling opacity never changes the page geometry.
        content.set_spacing(16)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        self.connection_help_button.set_margin_bottom(4)
        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(32, 32)
        self.spinner.set_halign(Gtk.Align.CENTER)
        self.spinner.set_opacity(0)
        content.append(self.spinner)

        self.connection_action_stack = Gtk.Stack()
        self.connection_action_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.connection_action_stack.set_size_request(240, 72)
        self._connection_button_css = Gtk.CssProvider()
        self._connection_button_css.load_from_data(
            "button.connect-primary { padding: 5px 12px; min-height: 0; font-weight: 600; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._connection_button_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.connect_button = Gtk.Button(label="Start device detection")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.add_css_class("connect-primary")
        self.connect_button.set_halign(Gtk.Align.CENTER)
        self.connect_button.set_valign(Gtk.Align.CENTER)
        self.connect_button.add_css_class("pill")
        self.connect_button.set_size_request(240, 46)
        self.connect_button.connect("clicked", lambda *_: self.connect_backend())
        self.connection_action_stack.add_named(self.connect_button, "connect")
        self.connected_mark = Gtk.DrawingArea()
        # Match the original success control's footprint until the connection
        # succeeds; the large illustration size is applied only in _connected.
        self.connected_mark.set_size_request(72, 72)
        self.connected_mark.set_halign(Gtk.Align.CENTER)
        self.connected_mark.set_valign(Gtk.Align.CENTER)
        self.connected_mark.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.connected_mark.set_tooltip_text("Device connected")
        self.connected_mark.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Device connected successfully"]
        )
        self.connected_mark.set_draw_func(self._draw_connected_mark)
        self.connection_action_stack.add_named(self.connected_mark, "connected")
        self.connected_controls = None
        content.append(self.connection_action_stack)

        self.connection_options_group = Adw.PreferencesGroup()
        self.connection_options_group.set_margin_top(12)
        self.connection_options_group.add(self.connection_options_row)
        content.append(self.connection_options_group)

        self.device_details = Gtk.Label()
        self.device_details.set_visible(False)
        clamp = Adw.Clamp(maximum_size=680)
        clamp.set_child(content)
        return self._scrolled(clamp)

    def _connection_options_panel(self):
        expander = Adw.ExpanderRow(
            title="Connection settings",
            subtitle="IoT mode, serial port, loader and preloader",
        )
        expander.set_show_enable_switch(False)
        self.iot_switch = Adw.SwitchRow(title="Use IoT mode")
        self.iot_switch.set_subtitle("Enable for supported MediaTek IoT devices")
        expander.add_row(self.iot_switch)

        self.serial_entry = Adw.EntryRow(title="Serial port (optional)")
        expander.add_row(self.serial_entry)

        for title, file_kind in (("Download Agent loader", "loader"),
                                 ("Preloader file", "preloader")):
            row = Adw.ActionRow(title=title, subtitle="Not selected")
            choose = Gtk.Button(label="Choose…")
            choose.set_valign(Gtk.Align.CENTER)
            choose.connect("clicked", lambda *_args, kind=file_kind:
                           self._choose_connection_file(kind))
            row.add_suffix(choose)
            expander.add_row(row)
            if file_kind == "loader":
                self.loader_row = row
                self.loader_button = choose
            else:
                self.preloader_row = row
                self.preloader_button = choose

        return expander

    def _navigation_menu(self):
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_tooltip_text("Main menu")
        button.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        menu = Gio.Menu()
        menu.append("Connect", "win.show-connect")
        menu.append("Firmware image matching", "win.show-identify")
        menu.append("Logs", "win.show-logs")
        menu.append("Settings", "win.show-settings")
        actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("show-connect", lambda *_: self.show_page("device")),
            ("show-identify", self._open_image_matching),
            ("show-logs", lambda *_: self.show_page("logs")),
            ("show-settings", self.open_preferences),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        self.insert_action_group("win", actions)
        button.set_menu_model(menu)
        return button

    def _open_image_matching(self, *_args):
        if self.identify_window is None:
            window = Adw.Window(
                transient_for=self, modal=True, title="Firmware image matching",
                default_width=780, default_height=660,
            )
            toolbar = Adw.ToolbarView()
            toolbar.add_top_bar(Adw.HeaderBar())
            box, clamp = self._page_column(760)
            box.append(self._page_heading(
                "Identify firmware images",
                "Search the selected folder and all subfolders. Matches are content-based; quick results are not full verification.",
            ))
            self.identify_folder_button = Gtk.Button(label="Choose Image Folder…")
            self.identify_folder_button.set_child(self._identify_action_content(
                "Choose Image Folder…", "folder-open-symbolic"
            ))
            self.identify_folder_button.set_hexpand(True)
            self.identify_folder_button.set_size_request(-1, 42)
            self.identify_folder_button.connect("clicked", self._choose_identify_folder)
            self.identify_force_button = Gtk.Button(label="Force Reindex")
            self.identify_force_button.set_child(self._identify_action_content(
                "Force Reindex", "view-refresh-symbolic"
            ))
            self.identify_force_button.set_hexpand(True)
            self.identify_force_button.set_size_request(-1, 42)
            self.identify_force_button.connect("clicked", lambda *_: self._index_identify_folder(True))
            self.identify_scan_button = Gtk.Button(label="Identify Partitions")
            self.identify_scan_button.set_child(self._identify_action_content(
                "Identify Partitions", "system-search-symbolic"
            ))
            self.identify_scan_button.add_css_class("suggested-action")
            self.identify_scan_button.add_css_class("pill")
            self.identify_scan_button.set_size_request(-1, 48)
            self.identify_scan_button.connect("clicked", lambda *_: self._scan_images(False))
            self.identify_verify_button = Gtk.Button(label="Full Verification")
            self.identify_verify_button.set_child(self._identify_action_content(
                "Full Verification", "emblem-ok-symbolic"
            ))
            self.identify_verify_button.add_css_class("pill")
            self.identify_verify_button.set_size_request(-1, 48)
            self.identify_verify_button.connect("clicked", lambda *_: self._scan_images(True))
            self.identify_cancel_button = Gtk.Button(label="Cancel")
            self.identify_cancel_button.set_icon_name("process-stop-symbolic")
            self.identify_cancel_button.connect("clicked", self._cancel_identify)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            actions.set_hexpand(True)
            actions.set_homogeneous(True)
            actions.append(self.identify_scan_button)
            actions.append(self.identify_verify_button)
            setup_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            setup_controls.set_hexpand(True)
            setup_controls.set_halign(Gtk.Align.FILL)
            setup_controls.set_homogeneous(True)
            setup_controls.append(self.identify_folder_button)
            setup_controls.append(self.identify_force_button)
            advanced_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            advanced_controls.set_halign(Gtk.Align.CENTER)
            advanced_controls.append(self.identify_cancel_button)
            self.identify_folder_button.set_sensitive(True)
            for button in (self.identify_force_button, self.identify_scan_button,
                           self.identify_verify_button, self.identify_cancel_button):
                button.set_sensitive(False)
            self.identify_cancel_button.set_visible(False)
            box.append(actions)
            box.append(setup_controls)
            self.identify_phase = Gtk.Label(label="No image folder selected", xalign=0)
            self.identify_phase.add_css_class("heading")
            self.identify_phase.set_ellipsize(Pango.EllipsizeMode.END)
            self.identify_phase.set_single_line_mode(True)
            self.identify_phase.set_hexpand(True)
            box.append(self.identify_phase)
            self.identify_progress = Gtk.ProgressBar()
            self.identify_progress.set_show_text(False)
            self.identify_progress.set_visible(False)
            box.append(self.identify_progress)
            self.identify_count = Gtk.Label(label="Choose an image folder to begin", xalign=0)
            self.identify_count.set_ellipsize(Pango.EllipsizeMode.END)
            self.identify_count.set_single_line_mode(True)
            self.identify_count.set_hexpand(True)
            self.identify_count.add_css_class("dimmed")
            box.append(self.identify_count)
            self.identify_results = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=8
            )
            self.identify_results.set_hexpand(True)
            self.identify_results.set_vexpand(True)
            self.identify_results.set_visible(False)
            self.identify_results_heading = Gtk.Label(label="Partition results", xalign=0)
            self.identify_results_heading.add_css_class("heading")
            self.identify_results.append(self.identify_results_heading)
            self.identify_results_summary = Gtk.Label(xalign=0)
            self.identify_results_summary.add_css_class("dimmed")
            self.identify_results_summary.set_single_line_mode(True)
            self.identify_results_summary.set_ellipsize(Pango.EllipsizeMode.END)
            self.identify_results_summary.set_hexpand(True)
            self.identify_results.append(self.identify_results_summary)
            self.identify_result_list = Gtk.ListBox()
            self.identify_result_list.set_selection_mode(Gtk.SelectionMode.NONE)
            self.identify_result_list.add_css_class("boxed-list")
            self.identify_results.append(self.identify_result_list)
            self.identify_results_note = Gtk.Label(xalign=0, wrap=True)
            self.identify_result_list.set_vexpand(False)
            self.identify_result_list.set_hexpand(True)
            self.identify_results_note.add_css_class("dimmed")
            self.identify_results.append(self.identify_results_note)
            box.append(self.identify_results)
            box.append(advanced_controls)
            toolbar.set_content(self._scrolled(clamp))
            window.set_content(toolbar)
            window.connect("close-request", self._hide_image_matching)
            self.identify_window = window
        self.identify_window.present()
        if not self.identify_restore_started:
            self.identify_restore_started = True
            if self.identify_directory:
                if os.path.isdir(self.identify_directory):
                    self._set_identify_status(
                        "Preparing firmware images", "Checking the selected folder"
                    )
                    self._index_identify_folder(False)
                    GLib.idle_add(self._watch_identify_directory)
                else:
                    self._set_identify_status(
                        "Image folder unavailable", "Choose another folder to continue"
                    )

    @staticmethod
    def _hide_image_matching(window):
        # Keep the popup and its progress/results widgets reusable on the next
        # menu activation; closing the window should not dispose its controls.
        window.set_visible(False)
        return True

    def show_page(self, name):
        if name == "device" or self.device_connected:
            if name == "device" and self.device_connected:
                self._show_connected_controls()
            self.stack.set_visible_child_name(name)

    def _show_connected_controls(self):
        if self.connected_controls is None:
            self.connected_controls = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=12
            )
            self.connected_controls.set_halign(Gtk.Align.CENTER)
            self.connected_controls.set_valign(Gtk.Align.CENTER)
            self.connected_controls.set_size_request(340, 280)
            connected_illustration = Gtk.Picture.new_for_filename(
                os.path.join(APP_DIR, "assets", "device-connected.svg")
            )
            connected_illustration.set_content_fit(Gtk.ContentFit.CONTAIN)
            connected_illustration.set_can_shrink(True)
            connected_illustration.set_size_request(300, 200)
            connected_illustration.set_accessible_role(Gtk.AccessibleRole.IMG)
            connected_illustration.update_property(
                [Gtk.AccessibleProperty.LABEL],
                ["Phone connected to a computer by USB"],
            )
            self.connected_controls.append(connected_illustration)
            connected_label = Gtk.Label(label="Device is currently connected")
            connected_label.add_css_class("title-1")
            self.connected_controls.append(connected_label)
            connected_subtitle = Gtk.Label(label="Ready to manage device storage.")
            connected_subtitle.add_css_class("dimmed")
            self.connected_controls.append(connected_subtitle)
            connected_actions = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            connected_actions.set_halign(Gtk.Align.CENTER)
            reconnect_button = Gtk.Button(label="Reconnect")
            reconnect_button.add_css_class("suggested-action")
            reconnect_button.add_css_class("pill")
            reconnect_button.connect("clicked", self._reconnect_device)
            connected_actions.append(reconnect_button)
            disconnect_button = Gtk.Button(label="Disconnect")
            disconnect_button.connect("clicked", self._disconnect_device)
            connected_actions.append(disconnect_button)
            self.connected_controls.append(connected_actions)
            self.connection_action_stack.add_named(
                self.connected_controls, "connected-controls"
            )
        self.connected_mark_started_at = None
        self.connection_illustration.set_visible(False)
        self.connection_status.set_visible(False)
        self.connection_help_button.set_visible(False)
        self.spinner.set_visible(False)
        self.connection_options_group.set_visible(False)
        self.connection_title.set_visible(False)
        self.connection_action_stack.set_size_request(340, 280)
        self.connection_action_stack.set_visible_child_name("connected-controls")

    def _disconnect_device(self, *_args):
        if self.operation_busy:
            self.toast_overlay.add_toast(Adw.Toast(
                title="Wait for the current operation to finish or cancel it before disconnecting"
            ))
            return
        backend = self.backend
        try:
            port = getattr(getattr(backend, "mtk", None), "port", None)
            close = getattr(port, "close", None)
            if close:
                close()
        except Exception as error:
            logging.warning("Error closing device connection: %s", error)
        self.backend = None
        self.device_connected = False
        self.brom_mode = False
        self.preloader_mode = False
        self.partitions = []
        self.partition_geometry = {}
        self._clear_partition_sources()
        self.show_partition_sources.set_sensitive(False)
        if self.connected_controls is not None:
            self.connection_action_stack.remove(self.connected_controls)
            self.connected_controls = None
        self.connected_mark.set_size_request(72, 72)
        self.connection_action_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )
        self.connection_title.set_text("Connect a MediaTek device")
        self.connection_title.set_visible(True)
        self.connection_title.remove_css_class("title-2")
        self.connection_title.add_css_class("title-1")
        self.connection_illustration.set_visible(True)
        self.connection_status.set_visible(True)
        self.connection_status.set_text("Connect a powered-off device to get started.")
        self.connection_help_button.set_visible(True)
        self.connection_options_group.set_visible(True)
        self.connection_action_stack.set_size_request(240, 72)
        self.connection_action_stack.set_visible_child_name("connect")
        self.connect_button.set_label("Start device detection")
        self._update_navigation_visibility()

    def _reboot_device(self, *_args):
        """Reboot through the active DA, or directly through BROM/preloader."""
        if (self.operation_busy
                or (self.connection_thread and self.connection_thread.is_alive())
                or (self.reboot_thread and self.reboot_thread.is_alive())):
            self.toast_overlay.add_toast(Adw.Toast(
                title="Another device operation is running"
            ))
            return

        self.reboot_button.set_sensitive(False)
        da_mode = self.device_connected
        if da_mode:
            self.append_log("Requesting a normal reboot through the active Download Agent session.")
        else:
            self.append_log("Starting a direct BROM/preloader reboot (Download Agent will not be started).")

        def reboot_worker():
            try:
                if da_mode:
                    # Use the exact Mtk instance configured by the active DA
                    # session. Do not reconnect or reinitialize the device.
                    mtk = self.backend.mtk
                    daloader = getattr(mtk, "daloader", None)
                    if daloader is None or getattr(daloader, "da", None) is None:
                        raise RuntimeError("The active DA session has no shutdown interface")
                    result = daloader.shutdown(bootmode=0)
                    if result is False:
                        raise RuntimeError("MTKClient DA shutdown command failed")
                    GLib.idle_add(self._reboot_finished, None, False, "DA")
                    return

                os.chdir(MTKCLIENT_DIR)
                if self.backend is None:
                    self.backend = self._prepare_backend()
                mtk = self.backend.mtk
                mtk.config.iot = self.iot_switch.get_active()
                # init() performs the MTKClient handshake and identifies the
                # chipset. Do not call DaHandler.configure_da() on this path.
                if not mtk.preloader.init():
                    raise RuntimeError("Could not establish a BROM/preloader session")
                config = mtk.config
                preloader = mtk.preloader
                watchdog = config.chipconfig.watchdog
                if not watchdog:
                    raise RuntimeError("This chipset has no supported watchdog reset register")
                watchdog_address, _ = config.get_watchdog_addr()
                if not watchdog_address:
                    raise RuntimeError("This chipset has no supported watchdog reset register")
                if not preloader.setreg_disablewatchdogtimer(config.hwcode, config.hwver):
                    raise RuntimeError("MTKClient could not disable the watchdog")
                # Do not call reset_to_brom(): it sets USBDL flags. This plain
                # watchdog reset lets the chip follow its normal boot selection.
                if not preloader.write32(watchdog_address + 0x14, 0x00001209):
                    raise RuntimeError("Watchdog reset register write was rejected")
                GLib.idle_add(self._reboot_finished, None, False, "BROM/preloader")
            except Exception as error:
                # Some devices drop USB as the watchdog reset takes effect,
                # before the write response can be read.
                disconnected = self._is_usb_disconnect_error(error)
                GLib.idle_add(
                    self._reboot_finished, str(error), disconnected,
                    "DA" if da_mode else "BROM/preloader",
                )

        self.reboot_thread = threading.Thread(
            target=reboot_worker, name="mtk-direct-reboot", daemon=True
        )
        self.reboot_thread.start()

    @staticmethod
    def _is_usb_disconnect_error(error):
        text = str(error).lower()
        return any(token in text for token in (
            "usb", "disconnect", "no device", "device has been disconnected",
            "read error", "write error", "timeout",
        ))

    def _reboot_finished(self, error, disconnected=False, reboot_mode="device"):
        self.reboot_button.set_sensitive(True)
        if error:
            self.append_log(f"{reboot_mode} reboot ended with: {error}")
            if disconnected:
                self.toast_overlay.add_toast(Adw.Toast(
                    title="USB disconnected during reset; device may be rebooting"
                ))
            else:
                self.toast_overlay.add_toast(Adw.Toast(
                    title=f"{reboot_mode} reboot failed: {error}"
                ))
        else:
            self.append_log(f"{reboot_mode} reboot command sent; waiting for normal device boot.")
            self.toast_overlay.add_toast(Adw.Toast(title="Reboot command sent"))
        if reboot_mode == "DA" and (not error or disconnected) and self.device_connected:
            # shutdown() closes/releases the DA port; reflect that in Fuse too.
            self._disconnect_device()
        return GLib.SOURCE_REMOVE

    def _reconnect_device(self, *_args):
        if self.operation_busy:
            self.toast_overlay.add_toast(Adw.Toast(
                title="Wait for the current operation to finish or cancel it before reconnecting"
            ))
            return
        self._disconnect_device()
        self.connect_backend()

    def _update_navigation_visibility(self):
        connected = self.device_connected
        # Keep the Adw.HeaderBar present while disconnected so the window's
        # client-side decorations and draggable title area remain available.
        self.header.set_visible(True)
        self.switcher.set_visible(connected)
        self.navigation_button.set_visible(connected)
        self.reboot_button.set_visible(connected)
        if not connected:
            self.stack.set_visible_child_name("device")

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

    @staticmethod
    def _identify_action_content(label, icon_name):
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_halign(Gtk.Align.CENTER)
        content.append(Gtk.Image.new_from_icon_name(icon_name))
        content.append(Gtk.Label(label=label))
        return content

    def _set_identify_status(self, phase, detail=""):
        if not hasattr(self, "identify_phase"):
            return
        self.identify_phase.set_text(phase)
        self.identify_count.set_text(detail)

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
        action_bar = Gtk.CenterBox()
        action_bar.set_margin_top(16)
        action_bar.set_margin_bottom(16)
        action_bar.set_margin_start(24)
        action_bar.set_margin_end(24)
        self.selection_count = Gtk.Label(label="0 selected")
        self.selection_count.set_xalign(0)
        self.selection_count.set_valign(Gtk.Align.CENTER)
        self.selection_count.set_margin_end(24)
        action_bar.set_start_widget(self.selection_count)
        action_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.read_button = Gtk.Button(label="Read…")
        self.read_button.connect("clicked", lambda *_: self._choose_directory("read"))
        self.choose_folder_button = Gtk.Button(label="Choose from folder…")
        self.choose_folder_button.connect("clicked", lambda *_: self._choose_directory("write"))
        self.write_button = Gtk.Button(label="Write…")
        self.write_button.connect("clicked", lambda *_: self._write_assigned_partitions())
        self.read_button.add_css_class("suggested-action")
        self.write_button.add_css_class("suggested-action")
        self.erase_button = Gtk.Button(icon_name="user-trash-symbolic")
        self.erase_button.set_tooltip_text("Erase selected partitions")
        self.erase_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Erase selected partitions"]
        )
        self.erase_button.add_css_class("destructive-action")
        self.erase_button.connect("clicked", lambda *_: self._confirm_erase())
        action_sizes = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for button in (self.read_button, self.write_button):
            button.add_css_class("pill")
            button.set_size_request(120, 44)
            action_sizes.add_widget(button)
            button.set_sensitive(False)
        self.choose_folder_button.add_css_class("pill")
        self.choose_folder_button.set_size_request(190, 44)
        self.choose_folder_button.set_sensitive(False)
        self.erase_button.add_css_class("pill")
        self.erase_button.set_size_request(44, 44)
        self.erase_button.set_sensitive(False)
        action_group.append(self.read_button)
        action_group.append(self.write_button)
        action_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        action_separator.set_valign(Gtk.Align.CENTER)
        action_separator.set_size_request(-1, 24)
        action_separator.set_margin_start(4)
        action_separator.set_margin_end(4)
        action_group.append(action_separator)
        action_group.append(self.erase_button)
        action_bar.set_center_widget(action_group)
        box.append(action_bar)
        self.operation_progress = Gtk.ProgressBar()
        self.operation_progress.set_show_text(True)
        self.operation_progress.set_visible(False)
        box.append(self.operation_progress)
        partition_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.partition_search = Gtk.SearchEntry()
        self.partition_search.set_placeholder_text("Search partitions")
        self.partition_search.set_accessible_role(Gtk.AccessibleRole.SEARCH_BOX)
        self.partition_search.set_hexpand(True)
        self.partition_search.set_tooltip_text("Search partitions (Ctrl+F)")
        self.partition_search.connect("search-changed", lambda *_: self._refresh_partition_view())
        partition_controls.append(self.partition_search)
        self.partition_sort = Gtk.DropDown.new_from_strings(
            ["Chip layout", "Name"]
        )
        self.partition_sort.set_tooltip_text("Sort partitions")
        self.partition_sort.connect("notify::selected", lambda *_: self._refresh_partition_view())
        partition_controls.append(self.partition_sort)
        box.append(partition_controls)

        self.select_all = Gtk.CheckButton(label="Select visible partitions")
        self.select_all.connect("toggled", self._toggle_all_partitions)
        self.select_all.set_sensitive(False)
        self.show_partition_sources = Gtk.CheckButton(label="Show image source")
        self.show_partition_sources.set_sensitive(False)
        self.show_partition_sources.connect("toggled", self._toggle_partition_sources)
        selection_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        selection_controls.append(self.select_all)
        selection_controls.append(self.show_partition_sources)
        self.dump_gpt = Gtk.CheckButton(label="Include GPT metadata when reading")
        self.dump_gpt.set_active(True)
        box.append(selection_controls)
        box.append(self.choose_folder_button)

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

        box.append(self.dump_gpt)
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
        tools_group = Adw.PreferencesGroup(title="Device tools")
        tools_group.set_description("Keys are saved locally. Lock state changes may affect device data.")
        tools_group.set_vexpand(False)
        self.key_button = Gtk.Button(label="Generate keys…")
        self.key_button.set_sensitive(False)
        self.key_button.connect("clicked", lambda *_: self._choose_key_directory())
        key_row = Adw.ActionRow(title="Device keys",
                                subtitle="Generate and save keys to a selected folder.")
        key_row.add_suffix(self.key_button)
        tools_group.add(key_row)
        self.unlock_button = Gtk.Button(label="Unlock bootloader…")
        self.unlock_button.set_sensitive(False)
        self.unlock_button.connect("clicked", lambda *_: self._confirm_lock_action("unlock"))
        self.lock_button = Gtk.Button(label="Lock bootloader…")
        self.lock_button.set_sensitive(False)
        self.lock_button.connect("clicked", lambda *_: self._confirm_lock_action("lock"))
        lock_row = Adw.ActionRow(title="Bootloader", subtitle="Changing lock state can erase device data.")
        lock_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        lock_actions.set_valign(Gtk.Align.CENTER)
        for button in (self.unlock_button, self.lock_button):
            button.set_valign(Gtk.Align.CENTER)
            lock_actions.append(button)
        lock_row.add_suffix(lock_actions)
        tools_group.add(lock_row)
        box.append(tools_group)
        return self._scrolled(clamp)

    def _utilities_page(self):
        # Use the tools page's scroller directly; wrapping it in a non-expanding
        # box caused the nested scroller to collapse when shown in the ViewStack.
        return self._tools_page()

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

    def _set_connection_phase(self, phrases, slow_search_phrases=None):
        """Show a short, phase-appropriate connection message and rotate it."""
        if self.connection_search_slow_source:
            GLib.source_remove(self.connection_search_slow_source)
            self.connection_search_slow_source = None
        self.connection_status_phrases = phrases
        self.connection_status_phrase_index = 0
        self.connection_status.set_text(phrases[0])
        if slow_search_phrases:
            self.connection_search_slow_source = GLib.timeout_add(
                15_000, self._show_slow_search, slow_search_phrases
            )
        return GLib.SOURCE_REMOVE

    def _show_slow_search(self, phrases):
        self.connection_search_slow_source = None
        if self.device_connected:
            return GLib.SOURCE_REMOVE
        self._set_connection_phase(phrases)
        return GLib.SOURCE_REMOVE

    def _rotate_connection_status(self):
        if not self.connection_status_phrases or self.device_connected:
            self.connection_status_source = None
            return GLib.SOURCE_REMOVE
        self.connection_status_phrase_index = (
            self.connection_status_phrase_index + 1
        ) % len(self.connection_status_phrases)
        self.connection_status.set_text(
            self.connection_status_phrases[self.connection_status_phrase_index]
        )
        return GLib.SOURCE_CONTINUE

    def _stop_connection_status_rotation(self):
        if self.connection_status_source:
            GLib.source_remove(self.connection_status_source)
            self.connection_status_source = None
        if self.connection_search_slow_source:
            GLib.source_remove(self.connection_search_slow_source)
            self.connection_search_slow_source = None
        self.connection_status_phrases = ()

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
        self._stop_connection_status_rotation()
        self.connect_button.set_sensitive(False)
        self.spinner.set_opacity(1)
        self.spinner.start()
        self.connection_illustration.set_visible(True)
        self.device_details.set_visible(False)
        self.connection_title.set_text("Connecting to your device")
        self._set_connection_phase((
            "Waking up the USB gremlins…",
            "Booting the tiny toolbox…",
            "Checking the cables of the digital kind…",
            "Tuning the device-detection antenna…",
            "Preparing for some very serious USB business…",
            "Getting our bits in a row…",
        ))
        self.connection_status_source = GLib.timeout_add(
            2600, self._rotate_connection_status
        )
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

            GLib.idle_add(self._set_connection_phase, (
                "Looking for your device…",
                "Listening for a MediaTek device on USB…",
                "Searching the USB neighborhood…",
                "USB sonar is on. Anything out there?",
                "Looking for a phone hiding in plain sight…",
                "The device-detection detective is on the case…",
                "Waiting for your phone to make its grand entrance…",
                "Scanning for tiny electronic footprints…",
                "Still looking. Make sure the phone is powered off…",
                "Checking every nook and cranny of the USB port…",
                "Calling all MediaTek devices…",
                "Our bits are listening. Is your phone ready?",
                "A powered-off phone is playing hide-and-seek…",
            ), (
                "No phone found yet; the plot thickens…",
                "Still no device detected. USB is keeping its secrets…",
                "Your phone is being very mysterious right now…",
                "Trying to intrepret the silence",
                "Somewhere, a bootloader is pretending not to hear us",
                "Taking the scenic route through USB…",
                "Still here, still searching, still oddly optimistic…",
                "Could the cable be charge-only? It happens to the best of us…",
                "The USB port owes us an explanation…",
                "Search status: hopeful, if a little puzzled…",
                "The phone has not RSVP’d to our USB party…",
            ))
            print("[mtkclient] Waiting for MediaTek USB device…", flush=True)

            # Follow the established Qt GUI connection sequence so the
            # preloader handshake is listening before the device is claimed.
            if not mtk.port.wait_for_device(max_attempts=1, wait=False):
                mtk.preloader.init()
            else:
                print("[mtkclient] USB device enumerated; performing handshake.", flush=True)

            GLib.idle_add(self._set_connection_phase, (
                "A device appeared. Initiating handshake…",
                "Knocking politely on the bootloader’s door…",
                "Exchanging secret USB handshakes…",
                "Making introductions to the bootloader…",
                "Negotiating politely with some very serious bits…",
                "Getting acquainted with your phone’s bootloader…",
                "The bootloader is considering our proposal…",
                "Establishing a tiny digital trust exercise…",
                "Hello, bootloader. We come in peace…",
            ))
            connected = self.backend.connect(mtk)
            if connected is None:
                raise RuntimeError("Backend could not initialize the device. See the log for details.")
            GLib.idle_add(self._set_connection_phase, (
                "Bootloader replied. Setting up the Download Agent…",
                "Loading the right tools onto the device…",
                "Almost there — preparing the device session…",
                "Unpacking the digital toolbox…",
                "Giving the Download Agent its marching orders…",
                "Teaching the device a few new tricks…",
                "Getting the Download Agent settled in…",
                "A little setup magic, strictly byte-sized…",
                "Making sure all the right bits are invited…",
                "The Download Agent is getting its badge…",
                "Setting the stage for the storage tour…",
            ))
            connected = self.backend.configure_da(connected)
            if connected is None:
                raise RuntimeError("Download Agent setup failed. See the log for details.")

            GLib.idle_add(self._set_connection_phase, (
                "Reading the device’s partition map…",
                "One last check: finding the storage layout…",
                "Taking a peek at the phone’s filing cabinet…",
                "Reading the storage map, one bit at a time…",
                "Asking the GPT where everything lives…",
                "Locating the device’s digital drawers…",
                "The storage tour is almost over…",
            ))
            _gpt_data, gpt = connected.daloader.get_gpt()
            if gpt is None:
                raise RuntimeError("Device connected, but its GPT partition table could not be read.")
            partitions = [(entry.name, entry.sectors * gpt.sectorsize)
                          for entry in gpt.partentries]
            geometry = {
                entry.name: {"name": entry.name, "size": entry.sectors * gpt.sectorsize,
                             "start": entry.sector * gpt.sectorsize,
                             "parttype": getattr(entry, "parttype", "user") or "user"}
                for entry in gpt.partentries
            }

            mode = ("Bootrom mode" if connected.config.is_brom else
                    "DA mode" if connected.config.chipconfig.damode else "Preloader mode")
            chipset = f"{connected.config.chipconfig.name} ({connected.config.chipconfig.description})"
            GLib.idle_add(self._connected, chipset, mode, partitions, geometry)
            print(f"[mtkclient] Connected: {chipset} — {mode}", flush=True)
        except Exception as error:
            logging.exception("MTK device connection failed")
            print(f"[mtkclient] Connection failed: {error}", file=sys.stderr, flush=True)
            GLib.idle_add(self._connection_failed, str(error))

    def _connected(self, chipset, mode, partitions, geometry=None):
        self._stop_connection_status_rotation()
        self.device_connected = True
        self.brom_mode = mode == "Bootrom mode"
        self.preloader_mode = mode == "Preloader mode"
        self._update_navigation_visibility()
        self.spinner.stop()
        self.spinner.set_opacity(0)
        self.connect_button.set_sensitive(True)
        self.connection_title.set_text("Device connected")
        self.connection_status.set_text("")
        # Once connected, turn the setup page into a quiet success state with
        # the illustration as its focal point.
        self.connection_illustration.set_visible(False)
        self.connection_status.set_visible(False)
        self.connection_help_button.set_visible(False)
        self.spinner.set_visible(False)
        self.connection_options_group.set_visible(False)
        self.connection_title.remove_css_class("title-1")
        self.connection_title.add_css_class("title-2")
        # Avoid crossfading the connect button against the badge: the two
        # controls overlap during the stack transition and briefly read as a
        # single transforming shape.
        self.connection_action_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.connected_mark.set_size_request(256, 256)
        self.connection_action_stack.set_size_request(280, 280)
        self.connection_action_stack.set_visible_child_name("connected")
        self.connected_mark_started_at = time.monotonic()
        self.connected_mark.queue_draw()
        GLib.timeout_add(16, self._animate_connected_mark)
        self.partitions = partitions
        self.partition_geometry = geometry or {}
        self._populate_partitions()
        self._selection_changed()
        self.operation_status.set_text(f"{len(partitions)} partitions available")
        self.storage_status.set_text("Device connected. Select a storage area.")
        self.tools_status.set_text("Device connected. Security tools are available.")
        for button in (self.raw_read_button, self.raw_write_button, self.key_button,
                       self.unlock_button, self.lock_button):
            button.set_sensitive(True)
        self.select_all.set_sensitive(bool(partitions))
        self.show_partition_sources.set_sensitive(bool(partitions))
        if self.identify_window:
            self.identify_folder_button.set_sensitive(True)
            for button in (self.identify_force_button, self.identify_scan_button,
                           self.identify_verify_button):
                button.set_sensitive(bool(self.identify_directory))
        return GLib.SOURCE_REMOVE

    def _animate_connected_mark(self):
        if self.connected_mark_started_at is None:
            return GLib.SOURCE_REMOVE
        progress = min(1.0, (time.monotonic() - self.connected_mark_started_at) / 0.78)
        self.connected_mark.queue_draw()
        if progress >= 1.0:
            self.connected_mark_started_at = None
            self.connection_success_timeout = GLib.timeout_add(250, self._show_flash_after_success)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _show_flash_after_success(self):
        self.connection_success_timeout = None
        if self.device_connected:
            self.stack.set_visible_child_name("partitions")
        return GLib.SOURCE_REMOVE

    def _draw_connected_mark(self, _area, context, width, height):
        context.save()
        context.scale(width / 256.0, height / 256.0)

        # The same restrained green gradient and rounded pale check as
        # assets/success-animated.svg, rendered natively so the check animates
        # consistently in GTK rather than relying on SVG SMIL support.
        gradient = cairo.LinearGradient(48, 37, 211, 224)
        for offset, color in (
            (0.0, (0x57, 0xd6, 0x8a)), (0.42, (0x33, 0xc8, 0x75)),
            (0.78, (0x26, 0xa2, 0x69)), (1.0, (0x20, 0x86, 0x53)),
        ):
            gradient.add_color_stop_rgb(offset, *(channel / 255 for channel in color))
        context.arc(128, 128, 101, 0, 2 * 3.141592653589793)
        context.set_source(gradient)
        context.fill()

        # A quiet highlight/shade overlay adds slight depth without extra decor.
        shade = cairo.RadialGradient(77, 70, 8, 128, 128, 130)
        shade.add_color_stop_rgba(0, 1, 1, 1, 0.16)
        shade.add_color_stop_rgba(0.57, 1, 1, 1, 0.035)
        shade.add_color_stop_rgba(1, 0.07, 0.25, 0.18, 0.09)
        context.arc(128, 128, 101, 0, 2 * 3.141592653589793)
        context.set_source(shade)
        context.fill()

        elapsed = (time.monotonic() - self.connected_mark_started_at
                   if self.connected_mark_started_at is not None else 0.78)
        progress = max(0.0, min(1.0, elapsed / 0.78))
        # Ease-out matches the SVG animation's clean, quick checked-on motion.
        eased = 1 - (1 - progress) ** 3
        context.move_to(77, 130)
        context.line_to(111, 164)
        context.line_to(180, 92)
        context.set_line_width(16)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        context.set_source_rgb(0xe8 / 255, 1.0, 0xe9 / 255)
        if eased > 0:
            context.set_dash((148 * eased, 148))
            context.stroke()
        context.restore()

    def _populate_partitions(self):
        if self._partition_menu_popover.get_parent() is not None:
            self._partition_menu_popover.popdown()
            self._partition_menu_popover.unparent()
        child = self.partition_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.partition_list.remove(child)
            child = following
        self.partition_checks.clear()
        self.partition_rows.clear()
        self.partition_pin_status.clear()
        self.partition_images.clear()
        self.partition_source_labels.clear()
        self.partition_sources.clear()
        self.partition_image_status = {}
        for name, size in self.partitions:
            row = Adw.ActionRow(
                title=name,
                subtitle=self._format_size(size)
            )
            image_status = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            image_status.set_tooltip_text("An image is assigned to this partition")
            image_status.update_property([Gtk.AccessibleProperty.LABEL], ["Image assigned"])
            image_status.set_visible(False)
            row.add_suffix(image_status)
            self.partition_image_status[name] = image_status
            source = Gtk.Label(label="No source found")
            source.add_css_class("dimmed")
            source.set_halign(Gtk.Align.FILL)
            source.set_hexpand(True)
            source.set_xalign(0.5)
            source.set_justify(Gtk.Justification.CENTER)
            source.set_wrap(True)
            source.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            source.set_tooltip_text("Source image for this partition, if identified")
            source.set_visible(self.show_partition_sources.get_active())
            row.add_suffix(source)
            self.partition_source_labels[name] = source
            self.partition_sources[name] = "No source found"
            pin_status = Gtk.Image.new_from_icon_name("view-pin-symbolic")
            pin_status.set_tooltip_text("Pinned to top")
            pin_status.update_property([Gtk.AccessibleProperty.LABEL], ["Pinned to top"])
            pin_status.set_visible(name in self.pinned_partitions)
            row.add_suffix(pin_status)
            self.partition_pin_status[name] = pin_status
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
            context_click = Gtk.GestureClick()
            context_click.set_button(Gdk.BUTTON_SECONDARY)
            context_click.connect("pressed", self._partition_context_pressed, name)
            row.add_controller(context_click)
            key_controller = Gtk.EventControllerKey()
            key_controller.connect("key-pressed", self._partition_row_key_pressed, name)
            row.add_controller(key_controller)
            row.set_tooltip_text(
                "Drop an image here, use Choose…, or right-click to pin"
            )
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
        # Prioritize the current task, then preserve the selected ordering
        # (device order or alphabetical) inside each priority group.
        def priority(name):
            check = self.partition_checks[name][0]
            if check.get_active():
                return 0
            if name in self.partition_images:
                return 1
            if name in self.pinned_partitions:
                return 2
            return 3

        names.sort(key=priority)

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
        self.show_partition_sources.set_sensitive(self.device_connected and visible_count > 0)

    def _toggle_partition_sources(self, check):
        visible = check.get_active()
        for label in self.partition_source_labels.values():
            label.set_visible(visible)

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

    def _partition_menu_model(self, name):
        menu = Gio.Menu()
        menu.append(
            "Unpin from top" if name in self.pinned_partitions else "Pin to top",
            "row.toggle-pin",
        )
        return menu

    def _partition_context_pressed(self, _gesture, n_press, x, y, name):
        if n_press == 1:
            self._show_partition_menu(name, x, y)

    def _partition_row_key_pressed(self, _controller, keyval, _keycode, state, name):
        if keyval == Gdk.KEY_Menu or (
            keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK
        ):
            row = self.partition_rows.get(name)
            if row is not None:
                self._show_partition_menu(
                    name, row.get_width() / 2, row.get_height() / 2
                )
            return True
        return False

    def _show_partition_menu(self, name, x, y):
        row = self.partition_rows.get(name)
        if row is None:
            return
        popover = self._partition_menu_popover
        popover.popdown()
        if popover.get_parent() is not None:
            popover.unparent()
        self._partition_menu_target = name
        popover.set_menu_model(self._partition_menu_model(name))
        popover.set_parent(row)
        rectangle = Gdk.Rectangle()
        rectangle.x = max(0, int(x))
        rectangle.y = max(0, int(y))
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def _toggle_partition_pin_action(self, *_args):
        name = self._partition_menu_target
        self._partition_menu_target = None
        if name is None:
            return
        self._partition_menu_popover.popdown()
        self._toggle_partition_pin(name)

    def _toggle_partition_pin(self, name):
        pinned = name not in self.pinned_partitions
        if pinned:
            self.pinned_partitions.add(name)
        else:
            self.pinned_partitions.discard(name)
        saved = True
        try:
            set_pinned_partitions(self.pinned_partitions)
        except OSError:
            saved = False
            logging.exception("Could not save pinned partitions")
            self.toast_overlay.add_toast(
                Adw.Toast(title="Pinned partitions could not be saved")
            )
        pin_status = self.partition_pin_status.get(name)
        if pin_status is not None:
            pin_status.set_visible(pinned)
        self._refresh_partition_view()
        if saved:
            self.toast_overlay.add_toast(Adw.Toast(
                title=f"{name} pinned to top" if pinned else f"{name} unpinned"
            ))

    def _selection_changed(self):
        count = len(self._selected_partitions())
        self._refresh_partition_view()
        self.selection_count.set_text(f"{count} selected")
        identification_running = self.operation_title in (
            "Identify firmware", "Verify firmware matches",
        )
        enabled = self.device_connected and count > 0 and (
            not self.operation_busy or identification_running
        )
        self.read_button.set_sensitive(enabled)
        self.choose_folder_button.set_sensitive(
            self.device_connected and (not self.operation_busy or identification_running)
        )
        self.write_button.set_sensitive(self.device_connected and count > 0 and (
            not self.operation_busy or identification_running
        ))
        self.erase_button.set_sensitive(enabled)

    def _choose_directory(self, operation):
        selected = self._selected_partitions()
        if not selected and operation != "write":
            self.toast_overlay.add_toast(Adw.Toast(title="Select at least one partition"))
            return
        dialog = Gtk.FileChooserNative.new(
            "Choose output folder" if operation == "read" else "Choose folder containing partition images",
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
        # Assigning an image is a clear signal that this partition is part of the
        # operation, so select it automatically for writing.
        image_check = self.partition_checks[name][0]
        if not image_check.get_active():
            image_check.set_active(True)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Image assigned to {name}"))

    def _write_assigned_partitions(self):
        selected = self._selected_partitions()
        if not selected:
            self.toast_overlay.add_toast(Adw.Toast(title="Choose images or select partitions first"))
            return
        missing = [name for name, _size in selected if name not in self.partition_images]
        if missing:
            self.toast_overlay.add_toast(Adw.Toast(
                title=f"Choose an image for: {', '.join(missing[:3])}"
            ))
            return
        images = {name: self.partition_images[name] for name, _size in selected}
        self._start_partition_write(selected, images)

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
                self.loader_row.set_subtitle(path)
                if self.backend:
                    mtk = self.backend.mtk
                    mtk.config.loader = path
                    mtk.daloader.daconfig.dasetup = {}
                    mtk.daloader.daconfig.parse_da_loader(path, mtk.daloader.daconfig.dasetup)
            else:
                self.preloader_path = path
                self.preloader_row.set_subtitle(path)
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
                                    total=sum(size for _name, size in selected))
            else:
                # Match every GPT partition against files anywhere under the
                # chosen folder. Prefer .bin when both formats are present;
                # sorted paths make duplicate matches deterministic.
                files_by_name = {}
                for root, dirs, files in os.walk(folder):
                    dirs.sort(key=str.casefold)
                    for filename in sorted(files, key=str.casefold):
                        stem, extension = os.path.splitext(filename)
                        if extension.casefold() not in (".bin", ".img"):
                            continue
                        files_by_name.setdefault(stem.casefold(), []).append(
                            (extension.casefold(), os.path.join(root, filename))
                        )

                images = {}
                for name, _size in self.partitions:
                    matches = files_by_name.get(name.casefold(), [])
                    if matches:
                        matches.sort(key=lambda match: (match[0] != ".bin", match[1].casefold()))
                        images[name] = matches[0][1]

                if not images:
                    self.toast_overlay.add_toast(Adw.Toast(
                        title="No matching .bin or .img partition images found"
                    ))
                else:
                    for name, filename in images.items():
                        self.partition_images[name] = filename
                        self.partition_image_status[name].set_visible(True)
                        self.partition_rows[name].set_subtitle(
                            f"{self._format_size(self.partition_checks[name][1])} · "
                            f"{os.path.basename(filename)}"
                        )
                        check = self.partition_checks[name][0]
                        if not check.get_active():
                            check.set_active(True)
                    self.toast_overlay.add_toast(Adw.Toast(
                        title=f"Assigned images to {len(images)} matching partition(s)"
                    ))
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
            if (self.operation_title in ("Identify firmware", "Verify firmware matches")
                    and title in ("Read partitions", "Write partitions")):
                self.pending_device_operations.append((title, callback, args, total))
                self.toast_overlay.add_toast(Adw.Toast(
                    title=f"{title} queued until partition identification finishes"
                ))
                return
            self.toast_overlay.add_toast(Adw.Toast(title="Another device operation is running"))
            return
        self.operation_title = title
        self._set_operation_running(True, title)
        self.operation_started_at = time.monotonic()
        self.operation_total = total
        self.operation_progress_position = 0
        self.operation_progress_offset = 0
        self.operation_has_progress = False

        def run():
            try:
                result = callback(*args)
                GLib.idle_add(self._operation_finished, title, None, result)
            except BaseException as error:
                logging.exception("%s failed", title)
                print(f"[mtkclient] {title} failed: {error}", file=sys.stderr, flush=True)
                GLib.idle_add(self._operation_finished, title, str(error), None)

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
            self.show_partition_sources.set_sensitive(False)
            for bar in bars:
                bar.pulse()
                bar.set_text("Starting…")
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title}… See Logs for details.")
            if title in ("Identify firmware", "Verify firmware matches"):
                self._selection_changed()
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
            self.show_partition_sources.set_sensitive(self.device_connected and bool(self.partitions))
            self._selection_changed()
        if hasattr(self, "identify_progress"):
            identify_task = title in (
                "Index firmware images", "Identify firmware",
                "Verify firmware matches",
            )
            show_progress = running and identify_task
            self.identify_progress.set_visible(show_progress)
            if show_progress:
                self.identify_has_progress = False
                self.identify_waiting_on_read = False
                self.identify_progress.set_fraction(0)
                self.identify_progress.pulse()
                self._set_identify_status(
                    "Verifying firmware" if title == "Verify firmware matches"
                    else "Identifying firmware",
                    "Preparing image checks",
                )
            elif not running:
                self.identify_progress.set_fraction(0)

    def _pulse_progress(self):
        if not self.operation_thread or not self.operation_thread.is_alive():
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.set_visible(False)
            return GLib.SOURCE_REMOVE
        if not self.operation_has_progress:
            for bar in (self.operation_progress, self.storage_progress, self.tools_progress):
                bar.pulse()
            if (getattr(self, "identify_progress", None)
                    and self.identify_progress.get_visible()
                    and not self.identify_has_progress):
                self.identify_progress.pulse()
        if (getattr(self, "identify_progress", None)
                and self.identify_progress.get_visible()
                and self.identify_waiting_on_read):
            self.identify_progress.pulse()
        return GLib.SOURCE_CONTINUE

    def _operation_finished(self, title, error, result=None):
        self.operation_title = None
        self._set_operation_running(False)
        if title in ("Identify firmware", "Verify firmware matches"):
            self.identify_cancel_button.set_sensitive(False)
            self.identify_cancel_button.set_visible(False)
            self.identify_progress.set_visible(False)
            self._set_identify_status(
                "Identification failed" if error else "Identification complete",
                "See Logs for details" if error else "Review the partition results below",
            )
        if error:
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title} failed. See Logs for details.")
            self.toast_overlay.add_toast(Adw.Toast(title=f"Operation failed: {error}"))
        else:
            for label in (self.operation_status, self.storage_status, self.tools_status):
                label.set_text(f"{title} completed")
            self.toast_overlay.add_toast(Adw.Toast(title=f"{title} completed"))
        if self.identify_pending_change and self.identify_directory:
            self.identify_pending_change = False
            GLib.timeout_add(100, self._reindex_after_change)
        if self.pending_device_operations:
            queued_title, callback, args, total = self.pending_device_operations.pop(0)
            GLib.idle_add(
                lambda: self._run_operation(queued_title, callback, *args, total=total)
            )
        return GLib.SOURCE_REMOVE

    def _choose_identify_folder(self, *_args):
        chooser = Gtk.FileChooserNative.new(
            "Select firmware image folder", self.identify_window or self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Select", "_Cancel",
        )
        chooser.connect("response", self._identify_folder_chosen)
        chooser.show()

    def _identify_folder_chosen(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file()
            if folder:
                self.identify_directory = folder.get_path()
                set_firmware_image_directory(self.identify_directory)
                self._set_identify_status(
                    "Preparing firmware images", "Scanning the selected folder"
                )
                self._clear_identify_results()
                self._clear_partition_sources()
                self._index_identify_folder(False)
                # Start discovery before touching old monitor state. A watcher
                # stack left by an earlier run can contain thousands of open
                # iterators; it must never delay first visible scan progress.
                GLib.idle_add(self._watch_identify_directory)
        chooser.destroy()

    def _index_identify_folder(self, force=False):
        if not self.identify_directory:
            return
        if self.identify_index_thread and self.identify_index_thread.is_alive():
            self.toast_overlay.add_toast(Adw.Toast(title="Firmware image indexing is already running"))
            return
        directory = self.identify_directory
        cancel_token = threading.Event()
        self.identify_cancel = cancel_token
        self.identify_generation += 1
        generation = self.identify_generation
        self.identify_progress_complete = False
        self.identify_refine_started_at = None
        self.identify_cancel_button.set_sensitive(True)
        self.identify_cancel_button.set_visible(True)
        self.identify_progress.set_visible(True)
        self.identify_progress.set_fraction(0)
        self.identify_progress.pulse()
        self._set_identify_status(
            "Scanning image folder", "Finding firmware images"
        )

        def index_worker():
            try:
                if self.identify_index is None:
                    self.identify_index = ImageIndex(APP_CONFIG_DIR / "firmware-images.sqlite3")
                count = self.identify_index.index_directory(
                    directory, force=force,
                    cancel=cancel_token,
                    progress=lambda done, total, name: GLib.idle_add(
                        self._update_identify_phase_progress, generation, done, total, name),
                    byte_progress=lambda path, number, image_count, done, total: GLib.idle_add(
                        self._update_identify_file_progress, path, number, image_count,
                        done, total, directory, generation),
                    discovery_progress=lambda path, found, dirs: GLib.idle_add(
                        self._update_identify_discovery, path, found, dirs, directory, generation),
                )
                GLib.idle_add(self._indexing_finished, generation, count, None)
            except BaseException as error:
                logging.exception("Firmware image indexing failed")
                GLib.idle_add(self._indexing_finished, generation, None, str(error))

        self.identify_index_thread = threading.Thread(
            target=index_worker, name="firmware-image-index", daemon=True
        )
        self.identify_index_thread.start()

    def _indexing_finished(self, generation, count, error):
        if generation != self.identify_generation:
            return GLib.SOURCE_REMOVE
        self.identify_progress_complete = True
        self.identify_cancel_button.set_sensitive(False)
        self.identify_cancel_button.set_visible(False)
        self.identify_progress.set_visible(False)
        if error:
            self._set_identify_status("Image indexing failed", "See Logs for details")
            self.toast_overlay.add_toast(Adw.Toast(title=f"Indexing failed: {error}"))
        else:
            self._identify_indexed(count or 0)
        return GLib.SOURCE_REMOVE

    def _identify_indexed(self, count):
        self.identify_progress_complete = True
        self._set_identify_status(
            "Ready" if count else "No firmware images found",
            f"{count:,} images available" if count else "Choose a folder containing .img or .bin files",
        )
        for button in (self.identify_force_button, self.identify_scan_button,
                       self.identify_verify_button):
            button.set_sensitive(bool(self.device_connected and count))
        return GLib.SOURCE_REMOVE

    def _update_identify_phase_progress(self, generation, done, total, message):
        if generation != self.identify_generation or self.identify_progress_complete:
            return GLib.SOURCE_REMOVE
        self.identify_has_progress = True
        if message.startswith("Comparing coarse fingerprints"):
            self.identify_refine_started_at = None
            self.identify_progress.set_fraction(0)
            self._set_identify_status(
                "Preparing image index", "Comparing firmware fingerprints"
            )
            self.identify_progress.pulse()
        elif message.startswith("Refining") or message.startswith("Fingerprinting"):
            if self.identify_refine_started_at is None:
                self.identify_refine_started_at = time.monotonic()
            fraction = min(1.0, done / total) if total else 0.0
            self.identify_progress.set_fraction(fraction)
            self._set_identify_status(
                "Improving image index", f"{done:,} of {total:,} fingerprint chunks"
            )
        elif message.startswith("Fingerprint refinement complete"):
            self.identify_progress.set_fraction(1)
            self._set_identify_status("Image index ready", "Firmware fingerprints updated")
            self.identify_refine_started_at = None
        elif total:
            self.identify_progress.set_fraction(min(1.0, done / total))
            self._set_identify_status(
                "Indexing firmware images", f"{done:,} of {total:,} images"
            )
        else:
            self.identify_progress.set_fraction(0)
            self._set_identify_status("Preparing image index", "Checking firmware images")
            self.identify_progress.pulse()
        return GLib.SOURCE_REMOVE

    def _update_identify_discovery(self, path, found, directories, root, generation):
        if generation != self.identify_generation or self.identify_progress_complete:
            return GLib.SOURCE_REMOVE
        self.identify_has_progress = True
        self._set_identify_status(
            "Scanning image folder", f"{found:,} images found in {directories:,} folders"
        )
        return GLib.SOURCE_REMOVE

    def _update_identify_file_progress(self, path, number, image_count, done, total,
                                       root, generation):
        if generation != self.identify_generation or self.identify_progress_complete:
            return GLib.SOURCE_REMOVE
        self.identify_has_progress = True
        if total:
            fraction = min(1.0, done / total)
            self.identify_progress.set_fraction(fraction)
            self._set_identify_status(
                "Reading firmware images", f"Image {number:,} of {image_count:,}"
            )
        else:
            self._set_identify_status(
                "Reading firmware images", f"Image {number:,} of {image_count:,}"
            )
        return GLib.SOURCE_REMOVE

    def _watch_identify_directory(self):
        if self.identify_monitor_walk_source:
            GLib.source_remove(self.identify_monitor_walk_source)
            self.identify_monitor_walk_source = None
        old_streams = self.identify_monitor_stack
        self.identify_monitor_stack = []
        old_monitors = self.identify_monitors
        self.identify_monitors = []
        if self.identify_poll_source:
            GLib.source_remove(self.identify_poll_source)
            self.identify_poll_source = None
        if old_streams or old_monitors:
            def dispose_old_watchers():
                for stream in old_streams:
                    try:
                        stream.close()
                    except OSError:
                        pass
                for monitor in old_monitors:
                    try:
                        monitor.cancel()
                    except GLib.Error:
                        pass
            threading.Thread(
                target=dispose_old_watchers, name="mtk-image-watch-cleanup", daemon=True
            ).start()
        if not self.identify_directory:
            return GLib.SOURCE_REMOVE
        try:
            root = Path(self.identify_directory)
            self._add_identify_directory_monitor(root)
        except OSError as error:
            logging.info("Unable to monitor image folder %s: %s", self.identify_directory, error)
        self.identify_monitor = self.identify_monitors[0] if self.identify_monitors else None
        # One watcher per folder is prohibitively expensive for firmware trees
        # containing source checkouts. Watch the selected root, and periodically
        # rescan metadata to catch changes in deeper descendants.
        self.identify_poll_source = GLib.timeout_add_seconds(
            120, self._periodic_identify_revalidation
        )

    def _add_identify_directory_monitor(self, path):
        try:
            monitor = Gio.File.new_for_path(str(path)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
            monitor.connect("changed", self._identify_files_changed)
            self.identify_monitors.append(monitor)
        except GLib.Error as error:
            logging.info("Image folder monitoring unavailable for %s: %s", path, error)

    def _periodic_identify_revalidation(self):
        if not self.identify_directory:
            self.identify_poll_source = None
            return GLib.SOURCE_REMOVE
        if self.operation_busy:
            self.identify_pending_change = True
        else:
            self._index_identify_folder(False)
        return GLib.SOURCE_CONTINUE

    def _identify_files_changed(self, *_args):
        self.identify_pending_change = True
        if self.identify_debounce:
            GLib.source_remove(self.identify_debounce)
        self.identify_debounce = GLib.timeout_add(900, self._reindex_after_change)

    def _reindex_after_change(self):
        self.identify_debounce = None
        if self.operation_busy:
            self.identify_pending_change = True
        elif self.identify_directory:
            self.identify_pending_change = False
            self._index_identify_folder(False)
        return GLib.SOURCE_REMOVE

    def _cancel_identify(self, *_args):
        if self.identify_cancel:
            self.identify_cancel.set()
            self._set_identify_status("Cancelling", "Stopping after the current read")

    def _update_identification_progress(self, partition, device_bytes, message, fraction):
        """Show a steady, concise phase and detail while matching firmware."""
        self.identify_has_progress = True
        reading = any(term in message for term in (
            "sample", "distributed validation", "full verification block", "verified through",
        ))
        waiting = fraction is None or reading
        self.identify_waiting_on_read = waiting
        full = self.operation_title == "Verify firmware matches"
        phase = "Verifying firmware" if full else "Identifying firmware"
        detail = "Preparing image checks"

        partition_match = re.match(r"Partition (\d+)/(\d+) · (.*)", message)
        if message.startswith("Validating indexed images"):
            phase, detail = "Preparing firmware images", "Checking image files"
        elif message.startswith(("Rechecking selected tree", "Reindexing changed image",
                                 "Checking image cache")):
            phase, detail = "Updating image index", "Checking the selected folder"
        elif "Comparing coarse image fingerprints" in message:
            phase, detail = "Comparing firmware images", "Choosing useful comparison samples"
        elif message.startswith("Found "):
            phase, detail = "Comparing firmware images", "Preparing image samples"
        elif "Checking fine fingerprints" in message:
            phase, detail = "Comparing firmware images", "Checking image samples"
        elif partition_match:
            number, count, activity = partition_match.groups()
            detail = f"Partition {number} of {count} · "
            if "preparing candidates" in activity:
                detail += "Preparing image candidates"
            elif "finding distinguishing offsets" in activity or "reusing the content-derived" in activity:
                phase = "Comparing firmware images"
                detail += "Comparing image samples"
            elif "full verification block" in activity or "verified through" in activity:
                detail += "Reading image data"
            elif "sample" in activity or "distributed validation" in activity:
                detail += "Reading device samples"
            elif "complete ·" in activity:
                detail += "Partition check complete"
            else:
                detail += "Checking image data"
        elif "sample" in message or "full verification block" in message:
            detail = "Reading device samples"
        self._set_identify_status(phase, detail)
        if fraction is None:
            self.identify_progress.pulse()
        else:
            fraction = max(0.0, min(1.0, float(fraction)))
            self.identify_progress.set_fraction(fraction)
        return GLib.SOURCE_REMOVE

    def _scan_images(self, full):
        if (not self.device_connected or not self.identify_index or self.operation_busy
                or (self.identify_index_thread and self.identify_index_thread.is_alive())):
            return
        directory = self.identify_directory
        image_index = self.identify_index
        partition_geometry = dict(self.partition_geometry)
        cancel_token = threading.Event()
        self.identify_cancel = cancel_token
        self.identify_cancel_button.set_sensitive(True)
        self.identify_cancel_button.set_visible(True)
        self._set_identify_status(
            "Verifying firmware" if full else "Identifying firmware",
            "Preparing image checks",
        )
        self._clear_identify_results()

        def scan_worker():
            mtk = self.backend.mtk
            daloader = mtk.daloader
            # Direct random-access reads through the already-authenticated DA.
            read_at = make_da_reader(
                daloader, partition_geometry, getattr(mtk.config, "pagesize", 512),
            )
            results = identify_images(
                image_index, list(partition_geometry.values()), read_at,
                full=full,
                directory=directory,
                cancel=cancel_token,
                progress=lambda name, count, message, fraction: GLib.idle_add(
                    self._update_identification_progress, name, count, message, fraction),
            )
            GLib.idle_add(self._show_identify_results, results)

        self._run_operation("Verify firmware matches" if full else "Identify firmware", scan_worker)

    def _show_identify_results(self, results):
        while child := self.identify_result_list.get_first_child():
            self.identify_result_list.remove(child)
        self.identify_results_note.set_text("")
        self.identify_results_note.set_visible(False)
        verified_count = sum(status.startswith("Verified") for _, _, status, _ in results)
        likely_count = sum(status.startswith("Likely match") for _, _, status, _ in results)
        review_count = sum(len(matches) > 1 or status.startswith("Unknown")
                           for _, matches, status, _ in results)
        summary = [f"{len(results)} partitions checked"]
        if verified_count:
            summary.append(f"{verified_count} verified")
        if likely_count:
            summary.append(f"{likely_count} likely matches")
        if review_count:
            summary.append(f"{review_count} need review")
        self.identify_results_summary.set_text(" · ".join(summary))

        for partition, matches, status, _coverage in results:
            if partition in self.partition_sources:
                if len(matches) == 1 and status.startswith(("Likely match", "Verified")):
                    source = os.path.realpath(os.path.join(
                        self.identify_directory, matches[0][0]
                    ))
                elif len(matches) > 1:
                    source = f"Ambiguous ({len(matches)} images)"
                elif status.startswith("Unknown"):
                    source = "Unknown"
                else:
                    source = "No match"
                self.partition_sources[partition] = source
                self.partition_source_labels[partition].set_text(source)
                self.partition_source_labels[partition].set_tooltip_text(source)
                self.partition_source_labels[partition].set_visible(
                    self.show_partition_sources.get_active()
                )
            geometry = self.partition_geometry.get(partition, {})
            if status.startswith("Verified whole-partition equality"):
                outcome = "Verified · whole partition"
                icon = "emblem-ok-symbolic"
            elif status.startswith("Verified image-content match"):
                outcome = "Verified image match"
                icon = "emblem-ok-symbolic"
            elif status.startswith("Likely match"):
                outcome = "Likely match · not fully verified"
                icon = "dialog-warning-symbolic"
            elif status == "Different":
                outcome = "No match found"
                icon = "dialog-information-symbolic"
            elif status.startswith("No size-compatible"):
                outcome = "No matching image"
                icon = "dialog-information-symbolic"
            elif status.startswith("Unknown"):
                outcome = "Could not confirm a match"
                icon = "dialog-warning-symbolic"
            else:
                outcome = status
                icon = "dialog-information-symbolic"

            warnings = []
            if status.startswith("Unknown:"):
                reason = status.split(":", 1)[1].strip()
                if "read failed" in reason:
                    warnings.append("Device read failed")
                elif "image changed" in reason:
                    warnings.append("Image files changed during matching")
                elif "no defined" in reason:
                    warnings.append("No readable image data to compare")
            if "; DONT_CARE regions unverified" in status:
                warnings.append("Some image regions could not be checked")
            if matches and any(size < geometry.get("size", size) for _name, size in matches):
                warnings.append("Partition tail not checked")
            if matches:
                if len(matches) > 1:
                    outcome += f" · {len(matches):,} possible images"
                else:
                    outcome += f" · {os.path.basename(matches[0][0])}"
            if warnings and matches:
                outcome += " · Review details"
            elif warnings:
                outcome += " · " + warnings[0]

            if matches:
                row = Adw.ExpanderRow(title=partition, subtitle=outcome)
                row.set_subtitle_lines(2)
                for name, size in matches[:10]:
                    candidate = Adw.ActionRow(
                        title=os.path.basename(name),
                        subtitle=f"{self._format_size(size)} · {name}",
                    )
                    candidate.set_subtitle_lines(2)
                    candidate.set_tooltip_text(name)
                    row.add_row(candidate)
                if len(matches) > 10:
                    row.add_row(Adw.ActionRow(
                        title=f"{len(matches) - 10:,} more candidates",
                        subtitle="Additional possible images were omitted from this list",
                    ))
                for warning in warnings:
                    row.add_row(Adw.ActionRow(title="Review", subtitle=warning))
            else:
                row = Adw.ActionRow(title=partition, subtitle=outcome)
                row.set_activatable(False)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.identify_result_list.append(row)

        if any(name.casefold() == "super" for name in self.partition_geometry):
            self.identify_results_note.set_text(
                "Logical partitions inside super are listed only when the device exposes them."
            )
            self.identify_results_note.set_visible(True)
        if not results:
            empty = Adw.ActionRow(
                title="No accessible partitions",
                subtitle="Connect a device and load its partition map",
            )
            self.identify_result_list.append(empty)
        self.identify_results.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _clear_identify_results(self):
        while child := self.identify_result_list.get_first_child():
            self.identify_result_list.remove(child)
        self.identify_results_summary.set_text("")
        self.identify_results_note.set_text("")
        self.identify_results_note.set_visible(False)
        self.identify_results.set_visible(False)

    def _clear_partition_sources(self):
        for partition, label in self.partition_source_labels.items():
            self.partition_sources[partition] = "No source found"
            label.set_text("No source found")
            label.set_tooltip_text("Source image for this partition, if identified")

    def _read_partitions(self, selected, folder, include_gpt=True):
        handler = self.backend
        handler.close = lambda: None
        self.operation_progress_offset = 0
        for name, _size in selected:
            filename = os.path.join(folder, f"{name}.bin")
            GLib.idle_add(self.append_log, f"Reading {name} to {filename}")
            args = SimpleNamespace(partitionname=name, filename=filename,
                                   parttype=None, offset=None, length=None)
            handler.handle_da_cmds(handler.mtk, "r", args)
            self.operation_progress_offset += _size
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
        self._stop_connection_status_rotation()
        self.spinner.stop()
        self.spinner.set_opacity(0)
        self.connect_button.set_sensitive(True)
        self.connection_action_stack.set_visible_child_name("connect")
        self.connection_title.set_text("Connect a MediaTek device")
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
