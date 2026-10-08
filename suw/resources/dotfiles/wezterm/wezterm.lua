-- SUW terminal profile: the same look on Ubuntu and macOS.
-- OSC 52 clipboard writes are enabled by default in WezTerm, which is what makes
-- "copy on a server, paste locally" work through SSH and tmux.
local wezterm = require("wezterm")
local config = wezterm.config_builder and wezterm.config_builder() or {}

config.font = wezterm.font_with_fallback({
  "JetBrains Mono",
  "Ubuntu Sans Mono",
  "SF Mono",
  "Menlo",
  "DejaVu Sans Mono",
})
config.font_size = wezterm.target_triple:find("darwin") and 13.0 or 11.5
config.line_height = 1.1
config.warn_about_missing_glyphs = false

config.colors = {
  foreground = "#e6edf3",
  background = "#0e1116",
  cursor_bg = "#5b9cf5",
  cursor_border = "#5b9cf5",
  cursor_fg = "#0e1116",
  selection_bg = "#264f78",
  selection_fg = "#e6edf3",
  split = "#30363d",
  ansi = { "#484f58", "#f85149", "#3fb950", "#d29922", "#5b9cf5", "#bc8cff", "#39c5cf", "#b1bac4" },
  brights = { "#6e7681", "#ff7b72", "#56d364", "#e3b341", "#79c0ff", "#d2a8ff", "#56d4dd", "#f0f6fc" },
  tab_bar = {
    background = "#0e1116",
    active_tab = { bg_color = "#161b22", fg_color = "#e6edf3" },
    inactive_tab = { bg_color = "#0e1116", fg_color = "#8b949e" },
    new_tab = { bg_color = "#0e1116", fg_color = "#8b949e" },
  },
}

config.use_fancy_tab_bar = false
config.hide_tab_bar_if_only_one_tab = true
config.tab_bar_at_bottom = true
config.window_padding = { left = 10, right = 10, top = 8, bottom = 6 }
config.window_decorations = wezterm.target_triple:find("darwin") and "RESIZE" or "TITLE|RESIZE"
config.animation_fps = 1
config.cursor_blink_rate = 0
config.audible_bell = "Disabled"
config.scrollback_lines = 50000
config.enable_wayland = true
config.check_for_updates = false
config.term = "xterm-256color"

return config
