-- Native bin-format probe. Run only in an empty project named MCRAWsome Bin Test.
-- Input media is synthetic. Only the bins created by this invocation are removed.
local function main()
    local project = resolve:GetProjectManager():GetCurrentProject()
    if not project or project:GetName() ~= "MCRAWsome Bin Test" then
        print("MCRAWsome: open an empty project named MCRAWsome Bin Test, then run this probe again")
        return
    end
    local base = os.getenv("MCRAWSOME_BIN_PROBE_DIR") or
        (os.getenv("HOME") .. "/.cache/mcrawsome-gpu-export/drb-probe")
    local pool = project:GetMediaPool()
    local previous = pool:GetCurrentFolder()
    local created = {}
    local function folder(label)
        local value = assert(pool:AddSubFolder(pool:GetRootFolder(), "mcrawsome bin probe " .. label .. " " .. os.time()), "Could not create test bin")
        created[#created + 1] = value
        assert(pool:SetCurrentFolder(value), "Could not select test bin")
        return value
    end
    local function clips(items)
        local result = {}
        for _, item in ipairs(items or {}) do result[#result + 1] = item end
        assert(#result > 0, "No test clips imported")
        return result
    end
    local ok, problem = pcall(function()
        for _, label in ipairs({"a", "b"}) do
            local bin = folder(label)
            local imported = clips(resolve:GetMediaStorage():AddItemListToMediaPool({base .. "/media-" .. label}))
            assert(pool:ExportMetadata(base .. "/native-" .. label .. ".csv", imported), "Could not export test metadata")
            assert(bin:Export(base .. "/native-" .. label .. ".drb"), "Could not export test bin " .. label)
        end
        folder("roundtrip")
        assert(pool:ImportFolderFromFile(base .. "/native-a.drb"), "Could not reimport native test bin")
    end)
    local restored = pool:SetCurrentFolder(previous)
    local removed = #created == 0 or pool:DeleteFolders(created)
    if not restored or not removed then
        print("MCRAWsome: test cleanup needs attention in MCRAWsome Bin Test")
    end
    if not ok then print("mcrawsome bin probe failed: " .. tostring(problem)); return end
    print("mcrawsome bin probe finished: " .. base .. "/native-a.drb and native-b.drb")
end
main()
