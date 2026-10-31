import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_resolve_bin.lua"
HARNESS = r"""
local real_io = io
local changes, restored, removed, exports = 0, false, 0, 0
local root, previous = {}, {}
local current = previous
local pool = {}
function pool:GetRootFolder() return root end
function pool:GetCurrentFolder() return current end
function pool:SetCurrentFolder(folder)
  current = folder; if folder == previous then restored = true end; return true
end
function pool:AddSubFolder(parent, name)
  assert(parent == root and not name:find('[^%w %-]'))
  changes = changes + 1
  local folder = {}
  function folder:Export(path)
    exports = exports + 1
    if FAIL and exports == 2 then return false end
    local f = assert(real_io.open(path, 'w')); f:write('test'); f:close(); return true
  end
  function folder:GetClipList() return {{}, {}, __flags = 99} end
  return folder
end
function pool:ExportMetadata(path, items)
  assert(#items == 2)
  local f = assert(real_io.open(path, 'w')); f:write('test'); f:close(); return true
end
function pool:ImportFolderFromFile(path) return true end
function pool:DeleteFolders(folders) for _ in ipairs(folders) do removed = removed + 1 end; return true end
local project = {}
function project:GetName() return WRONG and 'Owner project' or 'MCRAWsome Bin Test' end
function project:GetMediaPool() return pool end
local manager = {}
function manager:GetCurrentProject() return project end
local storage = {}
function storage:AddItemListToMediaPool(paths) assert(#paths == 1); return {{}, {}, __flags = 99} end
resolve = {}
function resolve:GetProjectManager() return manager end
function resolve:GetMediaStorage() return storage end
local env = os.getenv
os.getenv = function(key) if key == 'MCRAWSOME_BIN_PROBE_DIR' then return ROOT end; return env(key) end
io, require, os.execute, os.remove, os.rename = nil, nil, nil, nil, nil
"""


@pytest.mark.parametrize("mode", ["success", "failure", "wrong-project"])
def test_probe_only_changes_its_test_project_and_restores_selection(tmp_path: Path, mode: str) -> None:
    interpreter = shutil.which("luajit")
    if interpreter is None:
        pytest.skip("LuaJIT is unavailable")
    script = SCRIPT.read_text()
    harness = (
        f'ROOT = "{tmp_path}"; FAIL = {str(mode == "failure").lower()}; '
        f"WRONG = {str(mode == 'wrong-project').lower()}\n"
        + HARNESS
        + script
        + (
            "\nassert(changes == 0 and exports == 0)"
            if mode == "wrong-project"
            else "\nassert(restored and removed == changes and changes > 0)"
        )
        + ("\nassert(exports == 2)" if mode == "success" else "")
    )
    path = tmp_path / "harness.lua"
    path.write_text(harness)
    result = subprocess.run([interpreter, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
