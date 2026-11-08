-- switch.lua — key logger for `ffkit switch mark`.
-- Runs inside mpv while you watch the camera grid.
--   1-9      switch to that camera (logged with the current show time)
--   u        undo the last camera mark
--   s / e    mark the show's start / end at the current time
--   space    pause/play   [ ]  slower/faster   q  quit (resume later with the same command)
local mp = require 'mp'
local options = require 'mp.options'

local opts = { log = "switch.log", names = "" }
options.read_options(opts, "switch")

local names = {}
for pair in string.gmatch(opts.names, "[^|]+") do
  local k, v = pair:match("^(%d+):(.*)$")
  if k then names[k] = v end
end

local current = nil
local count = 0

local function fmt(t)
  local h = math.floor(t / 3600)
  local m = math.floor((t % 3600) / 60)
  local s = t % 60
  if h > 0 then return string.format("%d:%02d:%05.2f", h, m, s) end
  return string.format("%d:%05.2f", m, s)
end

local function banner()
  local label
  if current then
    label = "CAM " .. current
    if names[current] then label = label .. "  " .. names[current] end
  else
    label = "no camera yet - press 1, 2 or 3"
  end
  mp.set_osd_ass(1920, 1080,
    "{\\an9}{\\fs46}{\\b1}{\\bord3}{\\shad0}" .. label ..
    "{\\b0}{\\fs26}\\N" .. count .. " marks")
end

local function append(line)
  local f = io.open(opts.log, "a")
  if not f then
    mp.osd_message("cannot write " .. opts.log, 5)
    return false
  end
  f:write(line .. "\n")
  f:close()
  return true
end

local function mark(cam)
  local t = mp.get_property_number("time-pos")
  if t == nil then return end
  if append(string.format("%.3f,%s", t, cam)) then
    current = cam
    count = count + 1
    mp.osd_message(string.format("cam %s   @ %s", cam, fmt(t)), 1.2)
    banner()
  end
end

local function edge(kind)
  local t = mp.get_property_number("time-pos")
  if t == nil then return end
  if append(string.format("%s,%.3f", kind, t)) then
    local word = (kind == "START") and "start" or "end"
    mp.osd_message(string.format("show %s = %s", word, fmt(t)), 2)
  end
end

local function undo()
  local f = io.open(opts.log, "r")
  if not f then mp.osd_message("nothing to undo", 1.5) return end
  local lines = {}
  for line in f:lines() do lines[#lines + 1] = line end
  f:close()
  for i = #lines, 1, -1 do
    if lines[i]:match("^[%d%.]+,%d+$") then
      local removed = lines[i]
      table.remove(lines, i)
      local w = io.open(opts.log, "w")
      if w then
        w:write(table.concat(lines, "\n"))
        if #lines > 0 then w:write("\n") end
        w:close()
      end
      count = math.max(0, count - 1)
      current = nil
      for j = #lines, 1, -1 do
        local c = lines[j]:match("^[%d%.]+,(%d+)$")
        if c then current = c break end
      end
      mp.osd_message("undid  " .. removed, 2)
      banner()
      return
    end
  end
  mp.osd_message("nothing to undo", 1.5)
end

for i = 1, 9 do
  mp.add_forced_key_binding(tostring(i), "switch-cam" .. i, function() mark(tostring(i)) end)
end
mp.add_forced_key_binding("u", "switch-undo", undo)
mp.add_forced_key_binding("s", "switch-start", function() edge("START") end)
mp.add_forced_key_binding("e", "switch-end", function() edge("END") end)

mp.register_event("file-loaded", function()
  local f = io.open(opts.log, "r")
  if f then
    for line in f:lines() do
      local c = line:match("^[%d%.]+,(%d+)$")
      if c then current = c count = count + 1 end
    end
    f:close()
  end
  banner()
  mp.osd_message("1 2 3 = camera    u = undo    s / e = show start / end    space = pause    [ ] = speed    q = quit", 10)
end)
