# Contributor Instructions

- Treat `mtkclient/` as upstream/legacy code: do not edit it for the Adwaita client.
- Place all new GTK client files under `Adwaita/` and use Python + PyGObject + GTK 4/libadwaita.
- Read `GOALS.MD` before implementation and update `PROGRESS.MD` as work completes.
- Follow GNOME HIG patterns: native Adwaita containers and widgets, accessible labels/tooltips, non-blocking work, and explicit empty/loading/error states.
- Never imply device operations succeeded unless the existing backend reports success. Keep destructive operations gated until their backend integration and safety behavior are implemented.
