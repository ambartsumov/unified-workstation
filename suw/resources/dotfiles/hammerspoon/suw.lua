-- SUW on macOS: menu-bar status + global mode shortcut.
-- Loaded from the managed block in ~/.hammerspoon/init.lua, which sets SUW_BIN.
-- macOS conventions are respected: no window tiling, no Spaces manipulation.

local suw = SUW_BIN or (os.getenv("HOME") .. "/.local/bin/suw")
local menu = hs.menubar.new()
local snapshot = nil

local function run(args, callback)
  local task = hs.task.new("/bin/bash", function(code, stdout, _)
    if callback then callback(code, stdout) end
  end, { suw, table.unpack(args) })
  if task then task:start() end
end

local function line(text) return { title = text, disabled = true } end

local function render()
  if not menu then return end
  local s = snapshot
  if not s then
    menu:setTitle("○")
    menu:setMenu({ line("Workstation: starting…") })
    return
  end
  local work = s.mode ~= "DEFAULT"
  local warn = s.attention and #s.attention > 0
  menu:setTitle((work and "● WORK" or "○") .. (warn and " !" or ""))
  local items = { line((s.brand or "Workstation") .. " — " .. (work and "Work Mode" or "Default Mode")), { title = "-" } }
  for _, name in ipairs({ "home", "cloud" }) do
    local server = s.servers and s.servers[name] or {}
    local state = "not configured"
    if server.configured then state = (server.online == true) and "online" or (server.online == false and "offline" or "checking…") end
    table.insert(items, line(name:gsub("^%l", string.upper) .. ": " .. state))
  end
  if s.project then
    table.insert(items, { title = "-" })
    table.insert(items, line(s.project.name .. " · " .. (s.project.branch or "") .. " · " .. string.lower(s.project.status)))
  end
  if warn then
    table.insert(items, { title = "-" })
    for _, item in ipairs(s.attention) do table.insert(items, line("⚠ " .. item)) end
  end
  table.insert(items, { title = "-" })
  table.insert(items, { title = work and "Switch to Default Mode" or "Switch to Work Mode", fn = function() run({ "mode", "toggle", "--ask" }, refresh) end })
  table.insert(items, { title = "Sync now", fn = function() run({ "sync" }, refresh) end })
  menu:setMenu(items)
end

function refresh()
  run({ "status", "--json" }, function(code, stdout)
    if code == 0 then
      local ok, decoded = pcall(hs.json.decode, stdout)
      if ok and decoded then snapshot = decoded end
    end
    render()
  end)
end

-- Closest ergonomic equivalent of Super+D that collides with nothing on macOS.
hs.hotkey.bind({ "ctrl", "alt", "cmd" }, "D", function() run({ "mode", "toggle", "--ask" }, refresh) end)

suwTimer = hs.timer.doEvery(30, refresh)
render()
refresh()
