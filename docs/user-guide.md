# MCRAWsome user guide

MCRAWsome previews MotionCam RAW recordings (`.mcraw`) and Direct Log videos on Linux and prepares
RAW media for DaVinci Resolve. Select a clip in the Library, use **Resolve Settings** below its preview,
and send it as a native Media Bin or Timeline. Original recordings are kept.

MCRAWsome has no creative looks, grading, noise reduction or video encoding: it prepares recordings for
Resolve and other software. The command line exports CinemaDNG, OpenEXR and MCRAW copies. Settings saved
by earlier versions for looks, effects, noise reduction or video formats are ignored when they load.

This guide describes the Linux desktop interface and its background engine.

## Install and open

For a packaged release on Debian 13, install the `.deb` supplied with that release, replacing
the filename with yours:

```sh
sudo apt install ./mcrawsome_VERSION_amd64.deb
mcrawsome
```

The package installs MCRAWsome for all users, with its application menu entry and the `.mcraw` file
type. It uses Debian's own Python, FFmpeg, WebKitGTK and GStreamer, so Debian's updates reach them;
only the Python packages Debian lacks are installed with MCRAWsome. Opening a `.mcraw` recording with
MCRAWsome adds it to Open clips. Multiple selected recordings are passed together, and an already
running window receives them without starting another window or restarting its engine. Recordings
stay in place and their folders are not added to watched library folders. Existing default
applications are preserved; use your file manager's **Open With** menu to choose MCRAWsome if needed.

On other Linux systems, use the release's AppImage instead. It needs glibc 2.39 or later (Ubuntu 24.04,
Linux Mint 22, Fedora 40, Arch and later) and FUSE 3 for mounting, and carries everything else:

```sh
chmod +x mcrawsome-VERSION-x86_64.AppImage
./mcrawsome-VERSION-x86_64.AppImage
```

On first run it offers to add itself to your application menu and to open `.mcraw` recordings, for your
account only; run it with `desktop-entry` to do so later, or `desktop-entry --remove` to undo it. Run
`desktop-entry` again after moving or replacing the file. Mounted media and Resolve sends work as with
the Debian package; the AppImage's own FFmpeg decodes LOG recordings, with VAAPI or NVIDIA's NVDEC
where available.

The Debian package installs GNOME Files (Nautilus) actions for selected local `.mcraw`
recordings, in an **MCRAWsome** submenu: **Mount** or **Unmount**, **Send to Resolve (Media Bin)** and
**Send to Resolve (Timeline)** use the same saved clip settings as the interface. Restart Files after
installation to load its extension. The **Properties** page shows recording facts without decoding
frames.

The first item follows the interface's menu: **Mount** until a selected recording is mounted, wherever
its mount is, then **Unmount**, which acts on the selected recordings that are mounted. Open files in
an editor disable **Unmount**; Files menus have no hover text, so the recording's emblem (below) shows
which one is open. **Mount** prepares the media without opening the window or Resolve, starting the
engine if needed. **Unmount** unmounts every mount of each recording, such as full-resolution and proxy
mounts, the normal way. Both report what they did in a notification; media that an editor opened in the
meantime stays mounted, and is named. A copy of a mounted recording shows **Mount** and no emblem unless
the mount was made from that path; choosing **Mount** there reuses the same media. `mcrawsome mount` and
`mcrawsome unmount` do the same in a terminal; `unmount` says when a recording has nothing to unmount,
and starts no engine for it.

The Debian package also gives `.mcraw` recordings thumbnails in GNOME Files and other file managers
that use shared thumbnailers: the first frame, developed neutrally (camera white balance, no exposure
change) and as recorded, without a saved display turn. File managers run thumbnailers in a sandbox that
sees the recording alone, so the settings beside it cannot apply. Each thumbnail takes well under a
second, once: the file manager keeps it until the recording changes. GNOME Files shows thumbnails of
local files by default (**Preferences > Show Thumbnails**); restart Files after installing the package
(`nautilus -q`). The AppImage cannot provide them, because GNOME's sandbox sees only `/usr`.

Right-click MCRAWsome in the dock for **Library**, **Settings** and **Unmount All**. Unmount All
unmounts every recording mounted for Resolve the normal way and shows what it did in a notification;
media Resolve still has open stays mounted, and is named. `mcrawsome unmount-all` does the same in a
terminal.

Session files (`.mcrawsome-session.json`) open in MCRAWsome from the file manager and show its icon;
other JSON files keep their own applications. A running window loads the session as its own **Load**
does, saving pending clip changes first. MCRAWsome is also offered in **Open With** for MP4, MOV and
MKV files, without becoming their default, so a Direct Log recording opens from the file manager; a
video that is not a Log recording is refused with the reason.

In GNOME Files' list view, recordings can show **Duration**, **Resolution**, **Frame rate** and
**Camera** columns: right-click the column headings to add them. Recordings mounted for Resolve carry
an emblem, and a different one while Resolve has them open.

Typing in GNOME's Activities overview finds recordings in the library by file name and by their clip
details (title, description, keywords, notes, scene, shot, take, location and camera label), as the
Library's own search does; choose a result to open it. It answers from the library MCRAWsome last
scanned, so the engine need not be running. **Settings > Search** turns it off. Keyboard media keys
and GNOME's media controls play and pause the preview while a clip is open.

These come with the Debian package. After installing it, restart Files (`nautilus -q`) for the
columns and emblems, and log out and back in once for the search.

For a source install, follow the [README installation instructions](../README.md#install-development).
Build the interface with `npm --prefix web ci` and `npm --prefix web run build`, then run:

```sh
uv run mcrawsome gui
```

If `mcrawsome` is already on your PATH, use `mcrawsome gui`. For the native window, install the GUI extra with `uv sync --extra gui`; it also needs
GTK/WebKitGTK and their system dependencies. If the window cannot open, MCRAWsome reports why and opens the
interface in your default browser. `mcrawsome gui --browser` opens that browser directly. Browser
mode uses typed folder paths instead of native folder dialogues.

### Try the GTK 4 window (prototype)

The window normally uses GTK 3. A GTK 4 window, which draws the picture and the page on the graphics card
instead of repainting the whole window on the processor for each video frame, can be tried by closing
any MCRAWsome window (a second one only brings the first forward) and running, in a terminal:

```sh
MCRAWSOME_GTK=4 mcrawsome gui
```

The application icon still opens the GTK 3 window. The GTK 4 one needs GTK 4 and WebKitGTK 6.0
(`gir1.2-gtk-4.0` and `gir1.2-webkit-6.0`), which the package recommends, so apt installs them by default.
Without them the GTK 3 window opens. It has the GTK 3 window's features, though as a prototype its file
and folder dialogs and dropping files from Files have not yet been tried by hand. During 4K playback on a
HiDPI laptop it used a quarter of the processor time and ran about 18 °C cooler.

The engine normally listens at `http://127.0.0.1:8642`. Opening the interface starts it if needed,
or connects to the engine already running there. After an upgrade, a new engine adopts healthy
mount services from the old version. Each active mount retains its immutable runtime and saved
settings so open media remains available. New mounts use the new runtime.

## Add recordings to the Library

Choose **Library folders**, then in an empty Library select **Choose a folder** in the native window. In a browser, enter an absolute
**Folder path** and select **Add folder**. To add more folders, open **Settings**, use **Folders**,
and select **Add** after typing a path or using **Choose folder**. **Remove** removes a folder from
the saved list; it does not delete its recordings.

Copy from Card has been removed from the interface. Verified offloading remains available through
`mcrawsome copy` on the command line.

Use **Open clips** for temporary file references, separate from your watched library. This is the
default view when no library folders are configured. Drop local clips into its drop area or use
**Choose files** in the desktop app. Browser mode accepts full paths on the engine's computer; it
never uploads file contents. Only the chosen files are referenced, without scanning their neighbouring
folders or changing your Library settings. **Clear list** removes those references without deleting
files or unmounting media. The list lasts for the current engine session; library folders are saved.
Both views support the same preview, details, Resolve Settings and native Send actions.
Select one or more clips and choose **Remove from session** from a clip's context menu to remove
just those references.

**Unmount and Move to Trash...** in a clip's context menu moves recordings you no longer want to the
desktop's Trash, where the file manager can restore them; nothing is deleted. The confirmation lists
every library copy of each recording. Their virtual media is unmounted first, and nothing moves while
an editor has that media open. Each recording's gyro log (`.gcsv`), MotionCam's `.meta` and `.csv`
files and its saved settings (`.mcrawsome.toml`) go with it, and the clips leave the open session.
Resolve projects that use the mounted media show it offline. A split recording's parts
(`name.0.mcraw`, `name.1.mcraw`...) are separate clips: only the parts you choose move, and the
result lists the others that stay. Files on another drive go to that drive's own Trash folder. Dragging more files anywhere over the screen temporarily shows **Drop clips
here**. Moving the files away or dropping them restores the usual view and keeps your selection.

Use **New** to start an empty session, then add files in **Session clips**. **Save** writes the current
clip list and workspace to a `.mcrawsome-session.json` file. **Save As** chooses a new file; **Load**
and **Recent sessions** reopen one. The desktop app provides a file chooser; browser mode accepts an
absolute path on the engine's computer. Saving from Library folders or Open clips captures that
view's full clip list, including clips hidden by filters.

Sessions retain the selection, focused clip, name search, RAW/LOG choice, filters.
The workspace auto-saves after a short pause and restores at the next start. Once named, the session
file auto-saves too. **Save with relative paths** writes recording paths relative to the session file. Move the
session, recordings and their existing sidecars together, keeping their relative layout. Sessions
contain references and snapshots of effective clip recipes and annotations, up to 4,096 clips and
16 MiB. Loading restores the saved adjustments and details; recordings themselves are never copied.
Missing recordings are listed and their paths stay saved so a disconnected drive can return.
**Clear list** in Session clips clears its references without changing recordings or mounts.

The Library scans each folder and its immediate subfolders. It skips hidden and partial files,
DaVinci Resolve's own folders (`CacheClip`, `ProxyMedia` and `Resolve Project Backups`, which Resolve
keeps in its media storage location, often `~/Videos`), MCRAWsome exports with output sidecars, and
paths outside the allowed folders. It does not watch those folders either, so Resolve writing its
caches while you edit does not make the Library look again. Keep a RAW
clip's `.gcsv` gyro log beside it. Recordings from MotionCam 5.1 and later carry their gyro inside
the `.mcraw` and need no `.gcsv`. Direct Log videos can carry gyro data inside the video.
Folder chips marked **not found** usually mean a drive is disconnected or a folder has moved.

Each card shows a thumbnail, name, length, resolution and frame rate when available. **RAW** means
a MotionCam RAW clip; **LOG** means a video input. They appear before the resolution on the second line.
Move the pointer across a thumbnail to skim through the clip: ten pictures spread through it, with a line
at the pointer and the time there in place of the length. They are made when the pointer first rests on
the card, which takes a moment, and kept for next time.
The timing row reads **FPS: 24 | Frames: 512 | Dropped: 0 | Duplicated: 0** for RAW clips.
FPS is the rate the clip mounts at, to two decimals: within 0.2% of a standard rate it shows that rate (24, 23.98), otherwise the recorded rate (23.79); the detailed metadata keeps its full precision. LOG cards show
FPS and frame count when available, without RAW dropped or duplicated counts. Sensor bit depth,
white level and black levels remain available in the inspector rather than on the card.
The top-right **Clip settings** or **Defaults** label identifies the Recipe scope. Other badges are:

- **Recovered**: the recording was not finalised; MCRAWsome found frames by scanning it.
- **Dropped frames**: the recording has gaps. Exports normally repeat frames to keep sound aligned.

**Copying...** cards cannot be selected yet. A normal file must remain unchanged for about ten
seconds; a RAW recording without a final frame index waits ten minutes. An unreadable card shows
the error instead of offering actions.

Use **All**, **RAW** or **LOG**, **Search by name** and **Filter**. Clips stay in capture order, newest first. Click a card to select it. Ctrl-click toggles individual clips; Shift-click selects a
range in the displayed order. Enter or Space selects a focused card. The details panel follows the
focused selection and shows **Type**, **Format**, **Length**, **Captured**, **Camera** and **File**
when those facts are available.

Open **Filter**, beside All/RAW/LOG, to combine device, recorded resolution, frame rate, ISO, focal
length, sensor bit depth, capture date and stars. **Frame rate** offers whole rates: a clip recorded at
23.976 or 23.79 fps is under 24. **Date** offers **Today**, **This week** (from Monday)
and **This month**, using your computer's local calendar. These ranges exclude future or unknown dates
and stay relative when saved in a session. Individual capture dates keep their existing UTC labels.
Options come from cached Library facts;
filtering does not decode or reread recordings. **Unknown** selects clips without a recorded value.
**Clear filters** resets these facets while retaining search and RAW/LOG choices.

Each card's thumbnail fills its left side and shows the whole first frame, with a blurred copy behind
it filling any space left over. Each ready card has five stars and an **Unrated** control at the end of
the camera line, after the send status. Click a star, or focus a card and press
0 to 5. Outside text fields and controls, 0 to 5 rates the selected clips. Stars save in the clip's
existing annotations JSON alongside Good Take and Rejected, which remain independent tags.

**Unmount All** attempts every active mount, including clips outside the displayed list or
selection. Failures are shown for each affected mount. Media still held by an editor stays mounted and reports
the existing in-use message. Close those files and try again when they are no longer needed.

The checkbox above the cards selects every selectable clip in the filtered view. It shows a mixed state when only some visible clips are selected. Switching it off clears only that view's
selection, preserving selected clips hidden by a filter. Right-click, Shift+F10 or a card's three-dot
button opens the selection menu for sending, folder access, vignette profiles and unmount; selecting a clip already shows
it in Resolve Settings. Each card's selection checkbox is in its top-left corner. Mount, Unmount and Send are available
there and through the inspector. Open files in an editor disable Unmount; its explanation appears
on hover.

Under the preview, **Clip details** lets you log a title, description and comma-separated keywords.
Expand **Production details** for scene, shot, take, location, camera identifier, Tag and notes.
**Tag** selects exactly one of **Untagged**, **Good Take** or **Rejected**.
These are your editable notes; the recorded camera metadata and calibration displays remain separate.
Changes save automatically, with **Saving**, **Saved** or **Not saved** shown beside the heading.
**Save now** saves immediately. A failed save keeps the draft; **Discard edits and reload** explicitly
replaces it with the saved values. Titles do not rename the recording or its mounted media.

With several clips selected, the previous/next buttons above the preview move through that selection
without changing it. The active card has a thicker inset edge. **Save & next**, or Ctrl+Enter while
editing, saves the current clip, advances and focuses the next title. A failed save keeps you on that clip. Production details
stay expanded as you move through the selection. Space and Left/Right still control the video when
focus is outside the editor; they work normally as text-editing keys inside its fields. Inside the video's
own controls (play and step buttons, the Position bar, volume, the overlay and zoom menus' buttons),
Space still plays or pauses and Left/Right still skip five seconds while playing or step a frame while
paused; an open menu keeps its arrow keys, and a focused In or Out handle's arrows move that mark.

Details normally save as `recording.meta` beside `recording.mcraw` or a video with that basename.
Read-only folders and existing unrelated metadata files use MCRAWsome's local state instead; the editor
shows which storage is in use. **Save metadata** exports the portable document, and **Copy metadata** copies
it. Carry the metadata with its matching recording. Unknown extra fields in an existing MCRAWsome document
are retained. Only non-default fields are saved. Clearing every detail removes the empty sidecar.

**Send to Resolve** includes saved details in both native bins and paired timelines. It waits for
pending edits to save and stops if they cannot save. Titles become matching video and WAV display names; original
media paths stay the same. Notes map to Comments, and Camera maps to Camera #. Later edits apply to
the next send, without updating an existing Resolve project. The writer targets Resolve 21.1.1's
measured private format. Logging fields, Tag choices, custom titles and automatic capture fields
have been checked in both Resolve import formats and native re-exports. Search/Smart Bins and a full
project save/reopen remain separate checks. Keep keywords as separate
comma-delimited terms; native Send refuses terms whose escaping has not been verified.

A **Gyro** chip marks clips with a matching `.gcsv` log, motion recorded inside the `.mcraw` or
embedded telemetry. In the right-hand
clip details pane, expand **Gyro data** to see recorded angular velocity in degrees per second;
when acceleration is available, enable **Show acceleration** for metres per second squared. Toggle
X, Y and Z independently. Lines show means and shaded envelopes preserve minima/maxima from every
sample, in at most 256 groups. Axes are the recorded device axes before camera mapping; time starts
at the first IMU sample. Reading this graph does not decode RAW frames or run stabilisation. The
sidecar graph accepts logs up to 32 MiB and reports unreadable samples explicitly.

Media Bin and Timeline sends are available for every ready RAW clip. Timeline sends apply
stabilisation according to each clip's saved Stabilise switch and available gyro data.
A log being present does not establish correct synchronisation; sending validates it.

To correct a RAW clip's orientation, open **Resolve Settings → Orientation** and choose **Rotate 90° clockwise**,
**Rotate 180°**, or **Rotate 90° anticlockwise**. These turns are relative to **As recorded**. Wait for
**Saved**; the setting is kept in the clip's MCRAWsome recipe, and the original `.mcraw` stays unchanged.
Previews, Library thumbnails and developed exports use the turn. CinemaDNG exports and virtual media
carry it in their TIFF orientation tag, retaining the original sensor pixels. A changed orientation
creates a new virtual-media identity, preserving media already referenced by an editor. Send again
to use the new orientation; an existing Resolve timeline is not updated automatically.
Library thumbnails and the larger preview use the displayed aspect ratio, keeping the whole portrait
image visible. The recording metadata continues to report the original sensor dimensions.

**Resolve (Timeline)** sends saved display turns through
Resolve's **Clip Attributes → Image Orientation**. The Edit Inspector's Rotation Angle stays at zero.
The timeline references media mounted in its recorded orientation, with the saved RAW calibration
retained; neither the Bayer pixels nor the gyro corrections contain that extra display turn.
Resolve rotates the source image before fitting it to the timeline frame. The first clip's displayed
orientation determines that frame. With the default **DaVinci Resolve Free** setting, a quarter-turned
3840×2160 clip becomes a 1080×1920 timeline automatically, without a compensating Edit zoom. Free
preserves aspect ratio, caps portrait output at 1920 pixels high and landscape output at UHD, and
reports any downsizing. Enabling **Use DaVinci Resolve Studio** preserves the displayed source
resolution in either orientation without a downsizing warning. A batch uses the first clip's frame; gyro batches require matching displayed aspect ratios.
Your saved gyro crop still applies when stabilisation is enabled. Native **Resolve (Media Bin)** also
supports saved 90°, 180° and 270° display turns through Clip Attributes. Physical CinemaDNG export
continues to carry the turn in its TIFF tag.

When a known camera profile and a matching GCSV contain reliable acceleration readings, or the
recording carries its own motion and lens calibration, a clip can show **Check orientation**. Clip details and Resolve Settings explain the suggested turn. This uses gravity
near the beginning of the recording; gyro rotation rates alone cannot establish which way is up.
The check reads a bounded log prefix, requires mostly steady near-gravity measurements, and leaves
the indicator absent when the evidence is insufficient. Looking up/down, strong acceleration and
deliberate camera tilts can make the result ambiguous. Review the preview and choose Orientation
yourself; the indicator never rotates the recording automatically. It clears when the saved turn
matches the suggestion. The initial built-in mapping covers the Pixel 10 Pro XL 4K rear camera
modules tested with this workflow.

The gyro timing is measured against the picture on the stretch of the clip where the gyro shows
the camera turning most (`motion.sync_frames` frames, 480 by default), not its middle: while walking,
the camera's own travel and passers-by move the picture in ways the gyro cannot see. When the picture
still disagrees there, the five seconds of that stretch that turn most are checked on their own. The
`.drt.gyro` settings record the frames that confirmed the timing as `sync_frames`.

When the full correction of one direction (side to side, up and down, or roll) would add shake, that
direction keeps only the share of its correction that leaves the least: walking bobs the phone while
the hand tilts it, so the picture moves up and down less than the camera tilts, and removing all of
the tilt would show the bob. The send's notes say which direction was scaled and by how much, and the
settings record `gyro_axis_gains`; rolling-shutter rows in that direction are scaled with it.

A stabilised timeline clip always sends when it has a usable gyro log. When the picture cannot confirm the
gyro timing, for example a tripod shot or a steady walk with no turning to measure, or when the check
finds the correction working against the picture, the send still goes ahead with the fitted timing,
axes and focal length. The `.drt.gyro` settings then record `sync_confirmed: false` and the reason,
and the plan's notes say the picture could not confirm the timing. Possible causes of a disagreement
include timing or camera-axis calibration, parallax and moving subjects; rotating the display does not
change the RAW sensor-space motion used for synchronisation. Readable logs, gyro coverage of the clip,
source consistency, supported geometry, valid matrices and atomic archive writing are still required,
and a gyro strength of zero still refuses. Exports are never stabilised.
(A send from an older page asking for a gyro, unchecked or rolling-shutter timeline is treated as a
normal timeline send: each clip's Stabilise settings decide.)

**Rolling shutter compensation** (in Develop's **Stabilise** section, on by default) also corrects
rolling shutter, the skew and wobble from a sensor read top to bottom while the phone moves. In a
Resolve timeline it plans twelve row bands across the
camera profile's readout time and gives each its own pose in one of Resolve's twelve Camera Gyro row
slots, instead of repeating one pose. A probe render established that the slots run from the sensor's
first row to its last, evenly spaced, and that Resolve blends linearly between them
(`scripts/resolve_row_probe.py` writes that probe). The correction in Resolve is new, so compare it with
the switch off (one pose per frame) on your own footage. Resolve projects every pose with the synchronisation's
fitted focal length; when the lens's focal length is known, the row poses are spread so the rows
correct as much as that focal length calls for, while each frame's own pose stays as fitted. The
`.drt.gyro` settings record `rolling_shutter_rows: true` and the rows' focal length as `row_focal_px`. Stabilised timelines use full-resolution originals with any linked proxies; cropped media is refused.

Sending RAW clips to Resolve includes aligned WAV audio. Settings > DaVinci Resolve chooses
combined or separate video/audio bin items and whether separate items stay linked on timelines.
Either way Resolve reads the WAV from the clip's mount, beside its frames; nothing is copied.

To stabilise clips in Resolve, keep the MotionCam `.gcsv` beside its matching `.mcraw` recording.
Recordings from MotionCam 5.1 and later need neither a `.gcsv` nor a camera profile: they carry
their gyro, accelerometer and optical image stabilisation (OIS) lens shifts inside the `.mcraw`, on
the frames' own clock, with the phone's own lens calibration (focal length, optical centre, distortion,
rolling shutter and sensor orientation). Their timing needs no syncing, and the lens shifts are
compensated: with OIS on, the gyro alone would put back shake the lens had already removed. When a
clip has both, the recorded motion is used. A Recipe's camera overrides still win over the recorded
calibration.
In **Resolve Settings**, the **Stabilise** section holds the switch and the clip's **Mode**, **Crop**,
**Strength**, **Smoothing (seconds)** and **Rolling shutter compensation**; wait for **Saved**.
**Mode** chooses what the correction aims for: **Smooth** (the default) follows the camera's movement,
smoothed over the Smoothing time; **Rolling shutter only** straightens the wobble of the rolling shutter and
leaves the framing as shot (it needs Rolling shutter compensation on, and ignores Strength, Smoothing and
Horizon Lock); **Virtual tripod** holds one view for the whole clip; **Pan freely** holds tilt and roll
steady and lets the pan follow, smoothed (the pan is turning left and right on screen, so for a clip shown
turned 90 degrees it is the sensor's other axis). The tripod and pan hold the clip's average view. Where
the camera wanders further than the crop allows, those frames ease towards the camera's own path, and the
send says how many; a larger crop holds more. Return to the Library,
select the RAW clips, and choose **Send to Resolve (Timeline)** from the Send chooser or the context
menu. Every clip whose Stabilise switch is on and which has a gyro log is stabilised with its own
settings; the others are sent as recorded, in the same timeline. Planning a clip's stabilisation can
take a while, so the send's message says which stage it has reached: reading the clip's gyro, measuring
the picture to check the gyro, tracking image detail and planning the stabilisation, then writing the
timeline and opening it in Resolve. With several clips it names the clip, and the message's details
show each clip's own stage. If you switch to another window meanwhile, a desktop notification says when
the send has finished, or which clip failed. Complete the normal native `.drt` timeline import in Resolve. Keep MCRAWsome's virtual media mounted and the original recordings
available. **Resolve (Media Bin)** is never stabilised.

**Horizon Lock**, off by default in **Stabilise**, also levels the roll in a Resolve timeline send.
It needs accelerometer samples and a verified sensor-to-image mapping for the physical camera module.
The supported RAW mappings are recorded in the camera profile; the gyro's fitted motion axes alone do
not establish gravity. Verified physical axis mappings are reused across resolutions and frame rates
for the same identified module, including different RAW aspect ratios. This does not copy pixel focal
length, lens distortion, rolling-shutter readout or timing offsets from another recording mode. Unknown
modules and conflicting equally specific axis measurements remain unverified. It bridges short unreliable acceleration and refuses missing or prolonged
unreliable gravity. A camera pointing nearly straight up or down has no horizon to level, so the clip is
still stabilised and only those frames go without the lock: it eases out from 45 degrees of tilt, so the
picture never jumps, and within 30 degrees of straight up or down each frame keeps the camera's own roll.
Crop still limits the correction; the saved gyro report says when the lock was eased and how many frames
were not fully levelled. Manual display orientation
is respected. This option changes native timeline corrections, rather than baking pixels or changing
the current preview. It does not establish absolute heading or position.

**Review markers**, off by default in **Resolve Settings**, adds review suggestions to Timeline
sends. Its eight category switches select **Possible shake**, **Frequent focus changes**,
**Recorded tilt**, **Remaining tilt**, **Highlight clipping**, **Low recorded signal**, **Recording gap**
and **Motion blur** (green: shake blur of 20 pixels or more for half a second or longer, from the gyro
log and shutter, which stabilisation cannot remove).
Remaining tilt requires gyro and Stabilise; other categories remain available independently.
Recorded and remaining tilt use calibrated gravity with a five-degree warning sustained for one
second. Highlight and signal checks sample at most 120 RAW frames, so brief events can be missed.
Missing or unreliable sensor evidence is skipped. These suggestions can reflect intentional choices.
Bin sends omit markers. Send a new timeline after changing settings; imported timelines retain theirs.

**Crop** is the zoom Resolve applies (1.10 is a 10% zoom), which hides the edges the correction would
otherwise reveal. It is at least 1.05, since with no margin nothing can be corrected; settings saved
below that load at 1.05. While Stabilise is on, the preview dims the edges the crop removes and outlines
the frame that remains; hover over the dimmed edge for a reminder. The preview itself stays
unstabilised, but a few seconds after you open the clip or change a Stabilise setting, the outline
starts to move with each frame: MCRAWsome plans the clip as a timeline send would, and the outline then
shows where Resolve's kept frame lies on that frame, shifting and turning against the shake it removes.
With Image-assisted refinement on, a clip's first plan takes longest (about 15 seconds for a 27-second
4K clip), so a quick plan without the refinement moves the outline first and the refined one replaces
it when it is ready; the hover reminder says while it is refining. Until then, the outline stays centred.
When the clip cannot be stabilised with these settings it stays centred too, a line below the picture says
why, and **Stabilised** is unavailable. A timeline send of the clip with the same settings reuses that plan rather
than planning it again, and one sent while the preview is still planning waits for it. **Stabilised**,
in the strip above the picture, shows the picture as Resolve will stabilise it instead: each frame is
drawn so that its kept frame fills the picture. It is drawn from the same plan, so it takes effect once
the outline moves; switch it off to see the recording with the outline again. With Rolling shutter
compensation on, each frame's row correction is shown approximately, from its corners. A missing or unreadable gyro, insufficient coverage, failed synchronisation and
unsupported geometry stop the send with an explanation; a clip set to **on** with zero strength does
too. Analysis decodes
frames for synchronisation and can take longer than an ordinary native send. It changes no DNG pixels.
RAW defaults, clip order, video/audio pairing and recorded timing still use the normal native exporter.
Resolve's stabilisation mode is **Camera Gyro**. MCRAWsome supplies the complete computed pose cache
and its camera projection, including recorded pixel dimensions, estimated focal length in pixels
and your crop zoom. With rolling shutter compensation off, each frame has one correction repeated
across the native row slots.

If a virtual-media service has stopped, sending again reconnects the same mount automatically when
it is unused. A working mount is reused without interruption. A stopped mount's files are
unreadable already, so **Unmount**, **Remount** and sending again detach it even while Resolve still
holds them (as a file manager's eject does); the last two then serve the clip afresh. Relink or
reopen the clip in Resolve if it shows it offline. A mount whose service is running uses a normal unmount, so open
editor files still protect it.
Sending also checks that the virtual DNG files can actually be opened. An unused mount with an
unreadable service is reconnected automatically; a failure detected during timeline creation stops
the handoff with an explanation.

Resolve receives MCRAWsome's computed corrections. **Do not press Stabilize or change Resolve's
stabilisation controls to reanalyse the GCSV**: those actions may replace the supplied corrections,
and native reanalysis of the external log has not been demonstrated. For CinemaDNG, Camera Gyro can
be displayed as the imported active mode while absent from Resolve's selectable mode list. If you
change modes, send a new gyro timeline from MCRAWsome to restore its computed mode and corrections;
the exporter does not enable a new decoder or add that option to Resolve's menu. Read-only inspection
of Resolve 21.1.1 confirms that its selectable Camera Gyro option requires a BRAW source, readable
gyro samples from the BRAW decoder, and optical stabilisation not marked enabled. That check is
independent of the computed correction cache. To change the result, edit
MCRAWsome's motion settings and send a new timeline. The Send result gives the saved `.drt` path and
its adjacent `.drt.gyro` directory. Keep both: numbered clip folders preserve the original GCSV bytes,
full source plan and `settings.json` containing effective settings, camera profile, synchronisation,
source frame mapping and measurements for regeneration. A DRT still references the mounted RAW/WAV;
its gyro provenance is not a copy of those media.

This version targets the private Resolve 21.1.1 Camera Gyro format. It supports normal and
180-degree recorded orientation, with additional saved display turns in Clip Attributes,
matching whole-number timeline frame rates from 1–60 fps, and matching
displayed source/timeline aspect ratios. The first clip determines the frame orientation; portrait
frames use the sizing described above. With the default Free edition setting, a landscape timeline
is at most 3840 x 2160 in the clip's exact shape, for example 3840 x 2160 for 4096 x 2304 media.
The Studio setting preserves source resolution. The media
keep their full size. Many phones record a rate that is not a whole number (29.857 or 30.305 fps, say).
A timeline send checks every clip's rate before mounting anything, and names each clip at such a rate
with the nearest whole rate. **Use whole rates and send** (**Use 24 fps and send** when they share one)
sets **Mounted frame rate: Nearest whole rate** under RAW media in their Resolve Settings, then sends;
Media Bin sends use that rate too, and Cancel changes nothing. A clip whose nearest whole rate is above
60 fps can't go on a timeline: send it to the Media Bin, or give it a fixed Mounted frame rate of 60 or
less. Clips at different whole rates go on a timeline each, as clips of different displayed sizes do,
and each timeline's name gives its rate. Sending from the file manager names the clips without
offering the change. Other dimensions and aspect ratios have automated mathematical coverage. Native Camera Gyro render evidence includes the
researched orientation-3 sample (whose projection and pose records survived native re-export) and
48-frame comparisons on orientation-1 media at 3840×2160 landscape and 1080×1920 portrait. After fixing
an extra orientation conversion, Camera Gyro agrees with Perspective within 0.13 output pixels on
every matching frame. Those measurements used the previous Edit display turn; the gyro pose
conversion is unchanged. The current source-property route matches the native portrait reference
export byte-for-byte for the 90° rotation field. The integrated 1080×1920 Camera Gyro render matches
the verified landscape render after rotation and resizing across all 48 frames: median grid RMS
0.012 pixels, worst frame 0.134 pixels. The intended 1.1× gyro crop is retained without excess borders.
Earlier portrait builds could crop excessively or leave large borders because they turned the image
after timeline sizing. Source orientation now happens before sizing; the saved gyro crop remains
independent. Rolling-shutter row correction,
full lens-distortion correction, arbitrary retimes and older Resolve versions remain unverified or
unsupported. The
centred pinhole projection uses MCRAWsome's existing camera profile/focal estimation. Log gaps within
the existing 90% coverage threshold hold the nearest covered correction and are recorded in the notes.
The bounded native codec refuses caches above 8 MiB per clip instead of truncating long clips.

Native sends also include available recorded camera identity, ISO, aperture, exposure, received date
in UTC, focus estimate and captured sensor area. Lens Notes labels reported focal lengths as
**35 mm equivalent**. Camera Notes explains first-frame scope and derived source FPS, shutter angle
and white balance. The descriptive WAV rate/channel summary uses the actual paired output. These
automatic fields keep your production Camera #, Location and other logging fields unchanged.

When MotionCam's Extra Data → Post Process Settings contains a valid GPS fix, native bin and timeline
sends include labelled latitude/longitude in degrees, optional altitude in metres and the reported
GPS time in Resolve's **Camera Notes**. Your production **Location** stays separate. Empty zero-fix
defaults and invalid coordinates are omitted; missing altitude is not invented. GPS time and altitude
are preserved as recorded, without assuming a timezone or height datum. The right-pane GPS section
shows the same recorded values. Hover over metadata labels, values or clip-detail fields for their
meaning, units and source; unfamiliar raw fields are identified by their original key.

The capture-field mappings use the measured dictionary. Their display and units have been checked
with synthetic bin and timeline imports in Resolve 21.1.1.

Media preparation shows a completed-clip count and progress bar above the Library. Up to four
clips mount together; their selection order is preserved in the timeline. The indicator then
changes to the handoff stage while the archive is created and opened. A send can contain up to
64 RAW clips, subject to the same total limit for mounted and starting sequences.

New native sends also retain their mounted-media identities. Use **Mount** or **Remount** in the
clip's actions to make virtual media available again with its saved calibration. Once media is
unmounted (or gone after a reboot), its record is written to `mount-history.log` in MCRAWsome's state
folder and removed. Sending or mounting the same recording with the same settings again gives the
same media path, so a saved Resolve project finds it.
The original recordings are still required; use physical export for independent or archival media.

During sequential playback, each mount can prepare one RAW frame ahead on a private reader.
It starts after consecutive forward pixel reads; metadata-only browsing does not decode extra frames.
Random seeks use the foreground reader. This adds at most one speculative frame outside the pixel
cache and keeps the shared four-decode limit. RAW samples, calibration, timing and audio are unchanged.
Existing mounts keep their running service after an update: close the editor’s media handles, unmount
the test clip, restart MCRAWsome with the new build and mount it again to use the new reader.

Mounted media stays available when the MCRAWsome window closes. After reboot, open MCRAWsome and
mount or send the required clips again before opening your editor, or switch on **Settings >
Startup & background > Mount sent media again on start-up**: the engine then mounts the media you sent
to Resolve again, at the same paths and with the same settings, each time it starts, so your projects
find it online. Media you unmounted, and recordings that have changed or are offline, are left alone.
It does not start MCRAWsome at login: open MCRAWsome before your editor.

With a proxy chosen, **Full-size first frame** serves the first DNG of mounted media at full
resolution and the rest as proxies, so an editor that takes a sequence's size from its first frame
lists the clip at its recorded size. It is for media added from the mount folder; Send to Resolve
links full-resolution originals to its proxies instead and ignores it.

The **Readiness** rows explain what was found:

- **Gyro** reports whether there is a gyro log, not whether its timing is trustworthy.
- **Frames** reports dropped frames and how many will be filled on export.
- **Recovered**, when present, explains the recovery.
- **Camera profile** names a matching profile, or says **No camera profile**.

**Apply to Selected** in Resolve Settings copies the current clip's explicit orientation, RAW media, input-colour
and stabilisation adjustments to the other selected clips as per-clip overrides, with per-clip
results and retries for failures; its count includes the clip shown. **Set as Default** merges those
adjustments into the application's inherited defaults, which every clip uses, selected or not, unless
it or its folder has its own settings. Both actions preserve
trim and output choices. Session and Open clips selections support the same actions.

Select a clip to preview and prepare it directly in the Library's right pane. The pane shows the
clip's saved settings and In and Out marks, as they will be mounted, sent and exported. The compact
preview takes the video's shape. A slim strip along the top of the video holds the **Preview
overlay** menu at the left, and the **Zoom** menu (**Fit**, 25%, 50%, 75% or 100% actual size, one image
pixel per screen pixel) and fullscreen (Ctrl+F) at the right. The button beside fullscreen moves the
preview to where the clips are, larger, with the tabs beside it from the top; pressed again (its arrow
then points right) it puts the preview back and shows the clips. The choice is remembered. Four tabs
keep controls and information together; the end of the tab row names the clip (hover for its path) and
says whether its settings are saved:

- **Resolve Settings**: the clip's Source (orientation), Stabilise, Resolve (proxy), Review markers, RAW media and Advanced RAW settings, with Suggest trim, Apply to Selected, Set as Default and Reset above them.
- **Clip info**: three sections. **Info** has capture facts, recorded GPS and export readiness. **Analytics** has background capture analysis, source frame intervals, white balance, exposure, lens shading, sensor noise and RAW levels, plus recorded gyro/acceleration when available; it also works on LOG videos and clips without gyro. **Metadata** has calibration diagnostics and the recorded metadata tree, with hover explanations.
- **Notes**: editable title, description, keywords and production notes. Save & next advances through the selection.
- **Media**: virtual mount paths, current status, remount and unmount controls.

Each section folds to one line that says what it is set to, such as **Stabilise: Smooth · crop 1.10 ·
strength 1.00**; select the line to open or fold it. Stabilise starts open and the others folded, and
each section remembers being opened or folded. The Stabilise and Review markers switches sit in their
lines, so they work while folded.

In **Clip info**, **Recorded frame** gives the dimensions stored in the file; **Sensor dimensions**
gives the larger original sensor area when recorded. A crop from a 50 MP sensor can therefore contain
only 4K frames. MCRAWsome keeps those stored dimensions rather than inventing additional pixels.

Quad Bayer frames are remosaicked before development and CinemaDNG output. If an unbinned sensor
crop is missing its remosaic flag, MCRAWsome can recover it when frame zero has strong, consistent
2x2 colour-cell evidence. Ambiguous frames retain their recorded interpretation. The decision stays
fixed throughout the clip. Source MCRAW files are read without being modified.

In **Develop**, RAW **White balance** starts at the clip's median recorded neutral, reported as
temperature in Kelvin and tint. Temperature runs from 2000 to 12000 K; tint runs from -100 to +100.
Changing either control chooses one custom white balance for the whole clip. Some combinations are
outside what a particular camera's colour matrices can represent. The engine refuses those with
`recipe-invalid`; the message appears beside the sliders and the last valid setting stays in place.
Values are never clamped silently. **As Shot** returns to the clip's median recorded neutral.

Choose **Eyedropper**, then drag a rectangle over a neutral midtone in the preview. Picking pauses
playback and shows the Source frame so its rectangle follows the RAW crop and orientation, without
stabilisation changing its position. The engine measures camera RGB before exposure and the creative
grade. A rectangle that is mostly black or clipped is refused. Escape cancels; with the picker focused,
Enter samples a small rectangle at the centre. The result sets temperature and tint together for the
whole clip. A chosen white balance applies to previews, OpenEXR exports and exported DNGs'
`AsShotNeutral`. Mounted media and Resolve bins retain the recorded as-shot values.

The same controls are Recipe settings and CLI options (shown by `export --help-all`):

```sh
mcrawsome export clip.mcraw --develop.white_balance custom --develop.temperature 6000 --develop.tint 12
mcrawsome export clip.mcraw --pick-neutral 12,240,180,32,32
```

`--pick-neutral` takes `FRAME,X,Y,WIDTH,HEIGHT`, using the preview's zero-based output frame index and
full output pixel coordinates after RAW crop and orientation. It can be combined with `--dry-run
--json` to inspect the resulting custom Recipe before rendering. The API uses
`POST /api/v1/clips/{id}/white-balance` with `index`, `rectangle: [x,y,width,height]` and a `recipe`
table. `POST /api/v1/clips/{id}/white-balance/validate` checks a proposed Recipe against that camera.

Analytics starts a low-priority background metadata scan when opened. **Waiting**, **Scanning** and
frame progress distinguish queued work from ready results. Playback and navigation stay available;
leaving the tab stops polling but does not cancel the scan. Results are reused for the same source
path, file identity, size and modification time while the engine runs. **Refresh capture analytics**
explicitly rereads metadata. The queue holds at most four scans; up to eight results are retained.

RAW analysis seeks directly to the indexed JSON records and reads every recorded frame without
reading or decompressing image payloads. Graphs use at most 512 groups, including every valid
measurement in their means and extrema; groups with missing records leave gaps. **Save every frame
as CSV** exports individual measurements with exact integer timestamps. The JSON report contains
the bounded graph data and statistics. **Analytics → Graph** and the preview **Overlay** menu share
the same measurements when recorded: Gyro, Acceleration, Frame intervals, White balance, Lens shading,
Noise scale/offset, ISO, Shutter duration, Focus distance, Mean black level and White level. Analytics
also offers the combined RAW-level view and interactive shading heatmap. The graph menu includes:

- ISO and exposure duration (separate scales), and focus distance.
- Actual R/G/B `asShotNeutral` coefficients, not reciprocal gains or an estimated Kelvin value.
- Mean lens-shading gains for R, Gr, Gb and B. The heatmap loads the selected recorded map on demand,
  every 30 frames plus the last frame. Long recordings increase that stride to keep at most 256 maps.
  Channel selectors retain separate green channels; hover cells for exact gain values. These maps use
  sensor coordinates before display rotation or crop.
- Four sensor-noise scale and offset pairs, shown separately because their magnitudes differ.
  Three-channel DNG profiles are not relabelled as four Bayer channels.
- The four recorded black levels and white clipping level in RAW code values.

Timing includes every indexed interval, min/max, median, mean and standard deviation in microseconds.
On regular MP4/MOV LOG files it uses presentation timestamps from the movie sample tables, including
composition-time reordering; it does not invent timing from nominal FPS. RAW sensor coefficients cannot
be recovered from developed LOG pixels: unavailable fields are explained and omitted. Embedded gyro
and acceleration remain available when recorded. LOG preview and metadata analysis use the original
video and do not need a virtual mount. Fragmented movies and non-MP4/MOV files currently have no indexed
Analytics timing; their existing preview/export and telemetry fallback remain available.

The **Preview overlay** menu at the top left of the video adds a chart over the image. Its full trace starts faint, and becomes
opaque from left to right as the displayed frame advances, including backward seeks. Gyro, acceleration,
frame timing and available capture coefficients can be selected; **No overlay** removes it. RAW gyro overlays
use the existing receive-clock/profile offset, rather than running visual synchronisation. Capture
overlays sample up to 256 preview positions; use Analytics/CSV for all-frame changes. Overlays are only
viewer graphics and are not written into exported pixels or Resolve timelines. Native desktop playback
caches the chart drawing and advances it with the presented frame without encoding an extra video image.

Clip cards show the camera on its own line, icon-labelled capture facts, format pills and sensor/mount
chips. The **three-dot** button opens the same menu as right-click; opening it on an already selected
clip preserves the rest of the selection. Menus and workspace tabs include line icons. The menu keeps
Escape, Up/Down and Home/End navigation, and returns focus to its opener.

Analytics graphs show numbered axis ticks and grid lines. Hover to snap a vertical cursor to the
nearest displayed group and read all visible channels together, with their means and extrema. The
readout shows the recorded frame and timestamp when available. Focus the graph and use Left/Right,
Home/End or Escape for keyboard inspection. Hover values are grouped measurements; they do not
claim to be exact per-frame samples when a group contains multiple frames. Gyro time starts at the
first IMU sample. Its frame link uses receive-clock/movie timestamps and saved profile offsets,
without a visual synchronisation fit; missing clock links and lead-in/out are labelled unavailable.

Titles and field labels use the accent colour; values keep the regular text colour. Left/Right,
Home and End navigate the tabs from the keyboard. Switching tabs keeps the same preview session.
When multiple clips are selected, their previous/next strip sits above the right pane. It does not
move the clip list. The old Develop URL still works for bookmarks and opens Resolve Settings.
The Export action has been removed from the selection menu and clip inspector.

RAW clip cards show **Gyro** (violet, with a rotation icon) when a log exists and **GPS** (blue,
with a map pin) when a valid recorded fix exists. Empty GPS defaults do not produce a chip.

A clip's three-dot button and right-click menu share the same actions. Right-clicking a selected
clip keeps the other selected clips. **Open Folder** opens each distinct source folder in the
selection, including split recording parts. When a selected clip has active mounts, **Open Mount Folder**
opens their virtual CinemaDNG folders in the desktop file manager. In a browser without the desktop
bridge, folder actions are disabled.

Use **Mount** in the selection menu, right-click menu or three-dot menu to prepare CinemaDNG
and paired WAV without launching Resolve. The menu changes to **Unmount** while selected media
is mounted, disconnecting its active generations. Open files in an editor disable Unmount.
The inspector's **Media** tab also offers mounting and per-path recovery controls. On the cards,
**Mounted** is green, **Unmounted** is red, and mounts needing attention remain amber. These chips
sit alongside Gyro, GPS and other badges. Resolve settings save before
mounting or sending; a save failure stops the operation. Virtual media uses saved orientation, RAW
levels and gain-map calibration. The preview uses a neutral display conversion; creative settings
retained from older versions do not change it. Mounts keep their saved generation: changing settings and mounting again can create
a new path while previously sent media remains available. **Unmount** disconnects selected virtual media.

**Bake in Vignette Correction**, visible in Resolve Settings, chooses how virtual media carries lens
shading correction. Off (the default) keeps the sensor values untouched and stores the
correction as DNG gain maps, which only some editors apply. Turning it on corrects each
frame as the editor reads it, so the clip arrives already corrected, with no conversion step or wait
before import. A full correction raises the white level by four times (12-bit values become 14-bit) so
corrected corners do not clip; colour-only correction needs no extra range. The headroom comes from
the first frame; if a later frame needs more, its brightest corner values clip. Baked and untouched
media of the same clip are separate mount folders, so change the setting and mount or send again.

**Proxy**, visible in **Resolve Settings**, serves half-, quarter- or eighth-size virtual DNGs
for lighter editing. Sending to Resolve also mounts the full-resolution original and links the
smaller sequence as its native proxy in the DRB or DRT. Choose **Playback → Proxy Handling → Prefer
Proxies** in Resolve. For final rendering leave **Deliver → Advanced Settings → Use proxy media**
unchecked; output dimensions follow your timeline settings. Keep both retained mounts and the source
recording available. Existing mounts keep their settings, so send again after changing Proxy.

**Mounted frame rate** converts virtual media
to a constant rate by repeating and dropping frames, with the same choices as an export's frame rate:
the nearest whole or drop-frame rate, the average, the median (for slow motion) or 23.976 to 60 fps.
The sound keeps its recorded speed. **White / Black Levels** offers recorded, static and named white/black choices:
**Recorded per frame** bypasses fixed inherited levels.
**Use inherited levels** removes this clip's level overrides. Without folder or application overrides,
this uses each frame's recorded levels. **Static** uses frame 1's levels for every frame, and named
presets set both levels. **Mounted DNG crop** supports a custom rectangle placed on the preview, or removes a black strip on one edge (left by some 60 fps
stream configurations), found in the first, middle and last frames when mounting. **Camera named in
mounted DNGs** can name a Panasonic Varicam RAW or Blackmagic Pocket Cinema Camera 4K for other defaults
in Resolve's RAW tab, and the clip's name can be added so Resolve keeps each clip's RAW settings apart;
neither makes Resolve offer Camera Gyro for CinemaDNG. Gyro corrections use the full-resolution original; mounted cropping remains unsupported for
stabilised timeline clips. **Shading correction** also offers **Vignette only**, which corrects the falloff and leaves
colours as recorded. **Normalise exposure** has two modes. **To the
first frame's recorded exposure** scales each frame to the first frame's ISO times shutter as it is
read, evening out auto exposure flicker and timelapse exposure ramps with nothing to wait for; one stop
of headroom keeps darker frames from clipping, and changes in the scene's own brightness are kept.
**Measured from the picture** measures every frame's brightness once before
mounting, then brings each frame towards the average over **Smoothing window** (1 second by default):
flicker the metadata does not record (lights, the steps of a holy-grail timelapse) goes, slower changes
stay. Mounting waits for the measurement the first time (about a minute for ten minutes of 4K); the
result is kept, so mounting again is immediate. The headroom is the largest brightening rounded up to a
power of two, at most two stops; a frame brightened further clips there, and darkened frames never
clip. For a physical CinemaDNG export, `--develop.deflicker 1 --output.dng_deflicker baked` writes the
same values. **Transfer curve in mounted DNGs** set to **Log** serves log-encoded values with a
linearisation table, exactly as a log CinemaDNG export stores them: **Bits removed** takes 2 to 8 bits off with dithering, and **Auto** serves the sensor's bits
less 2. Codes of 8 bits or fewer take one byte each, so the editor reads half as much; every DNG reader
turns the codes back into linear values. Fewer than 6 bits is refused. Each of these settings makes its
own mount folder.

Sent clip cards show **In use** while virtual files or folders are open, **Idle** when there
are no open handles, and **Unmounted** after disconnection. Resolve opens a clip's frames only while it
reads them, but keeps the audio of a clip it has loaded open, so that clip stays **In use** while Resolve
has it loaded, not just while it plays. Clips sent by builds before revision 71 point Resolve at a copy
of the WAV and show **In use** only while it reads frames; send them again to change that. A file
manager window open on a mount folder also keeps it in use. Changes arrive through the live
event stream. Idle media can still be referenced by a saved editor project. Older mount services
show **Usage unavailable** until remounted with a build that reports usage. Unmount is disabled
while use is reported, and the operating system checks again before disconnecting the media.
**Virtual media** labels each path as **Sent media** or **Prepared media**, with its saved display turn.
A rotated native bin or timeline uses **Sent media · Recorded orientation** and applies its rotation in
Resolve. The list refreshes after sending so this dependency appears immediately. **Remount** is
available for idle media even when it says Mounted: it reconnects that exact path with its saved
calibration. Keep sent media mounted while using it in Resolve. If its source changed or is offline,
Remount explains that the source must be restored or the current clip sent again.

For RAW clips, **Export Vignette Profile...** saves the focused selected RAW clip's lens-shading map
as JSON. Its name is shown as the source; if the focused clip is LOG, the first selected RAW clip is
used. **Apply Vignette Profile to Selected...** and **Clear Vignette Profile Override** act on the
selected RAW clips and report each result separately. Profiles need matching sensor dimensions and
Bayer layout. Unmount a clip before changing its profile. Clearing removes the clip's override and
returns to its inherited or recorded map. Other adjustments and the source recording are retained.
The desktop uses native file dialogues; a browser downloads or uploads JSON files up to 2 MiB.

Profiles affect the Library preview, Resolve Settings, developed exports and CinemaDNG gain maps or baked
shading. Virtual media and queued exports retain the profile snapshot they were created with.
Photo processing uses **photo.flat_fields** instead.

Expand **Calibration** to see recorded gain maps for all four Bayer planes on a shared gain scale,
plus the recorded noise model when available. The plots use the first frame's metadata, in sensor
orientation, and are drawn only while the foldout is open. Large maps are sampled for display;
the exact dimensions, gains and noise coefficients remain available in the recorded fields below.
In **Capture analytics**, the lens-shading heatmap's **Flat field** view shows what an evenly lit grey
card looks like through the lens before correction: darker where it vignettes, tinted where one colour
falls off faster.

Expand **RAW diagnostics** for measurements made only when requested. **Read frame timing** reads
the recording index without decoding images. The plot shows minimum/maximum interval envelopes and
means in at most 256 groups, preserving gaps in long recordings. The dashed line is the median.
Missing-frame and short-interval counts describe the source before output frame-rate conversion.

**Measure first RAW frame** counts every photosite in recorded frame 1 before black subtraction,
shading, white balance or exposure. Four Bayer histograms share a RAW-code axis; **Log counts**
makes small populations easier to see. The table gives exact recorded black/white levels, sample
counts, minima/maxima and counts at or beyond those levels. These thresholds use the recorded levels,
independent of display bins or Recipe overrides. This measures one frame, not the whole recording or
the current preview position. Quad Bayer frames are measured after their remosaic (below).

**Quad Bayer recordings.** Some phones' sensors put each colour filter over 2x2 photosites (Quad Bayer)
and, read at full resolution, MotionCam marks their frames as needing remosaic. MCRAWsome rebuilds each
such frame as an ordinary Bayer mosaic of the same size as it reads it (automatic Quad Bayer remosaic), so previews, exports, CinemaDNG and mounted DNGs all work as for any recording; `MCRAWsome
info` and the export notes say so. MCRAW copies keep the recorded frames. On test scenes it keeps
neutral detail almost as a Bayer sensor would, with little false colour; fine colourful detail is softer,
as the sensor samples colour more coarsely. A real unflagged GBRG sensor crop has also been checked.
Remosaic takes about 1.4 seconds a 4K frame, so previews and mounted playback of such clips are slow.

**Settings > Processing > RAW processing > Quad Bayer Remosaic** is enabled by default. It automatically processes
marked frames and strongly detected unbinned sensor crops, using the recording's GBRG, RGGB, GRBG or
BGGR pattern. Ordinary Bayer recordings are unchanged. Switch it off to use the recorded photosites
without remosaic. Reopen a clip to change its preview; new exports and mounts use the saved choice.
Existing exports and mounted media retain their original choice. A resumed export restarts if the
choice changed, and thumbnails are cached separately for each choice.

**Colour filter.** For a recording whose metadata is wrong, **Advanced RAW settings > Colour filter**
sets how one clip is read, overriding the preference above. **Layout** reads it as Quad Bayer (always
remosaicked from 2x2 cells, for a missing or wrong flag) or as Bayer (the photosites as recorded).
**Bayer order** replaces the recorded RGGB, BGGR, GRBG or GBRG order when colours swap or the picture
turns magenta and green. Both apply to the preview, exports and new mounted media.

**Colour matrix.** **Advanced RAW settings > Colour matrix** takes nine numbers, row by row, mapping
XYZ to the camera's colours (DNG ColorMatrix). They replace the recording's matrices for both
illuminants in the preview, exports and mounted DNGs (the `camera.colour_matrix` setting). Empty
the field to use the recording's again.

**Scale values to 16 bits** (Virtual CinemaDNG) multiplies the served values so the white level is
65535, with the black levels in proportion. The picture is unchanged; some editors handle a full
16-bit range better than a 10- or 12-bit one. It needs linear mounts, since log codes already fill
their range.

Pause playback before measuring. Starting playback, closing the foldout or selecting another clip
cancels a pending measurement. One background diagnostic runs at a time, with no pending queue.
A RAW frame decode already in progress must finish before cancellation takes effect; counting stops
between bounded blocks. No diagnostic starts automatically when opening a clip or the foldout.

Use **Copy JSON** or **Save JSON** beside calibration, recorded metadata and diagnostic results to
retain their full available values. Timing JSON contains group aggregates, rather than every original
timestamp. Histogram JSON includes all display-bin edges/counts and exact threshold counts. The
desktop opens a save dialogue; a browser downloads JSON. If clipboard access is unavailable, a
selected text field provides the JSON for manual copying. Saved JSON reports are limited to 8 MiB.

<!-- screenshot: Library cards, a selected clip and its Readiness rows -->

### Preview chart overlays

While a menu is open over a playing video, the picture keeps playing above or below it, and the band of
the picture level with the menu holds still until the menu closes.

Choose the **Preview overlay** menu (the chart icon at the top left of the video) to show Gyro,
acceleration, frame intervals, ISO, shutter, focus, white balance, shading/noise coefficients or RAW
black/white levels over the picture. **No overlay** clears it. Only available charts are enabled after
the first metadata read. No analysis is started until an overlay is chosen.

The complete clip's trace is faint; the portion up to the displayed frame is bright, with a white
position marker. Seeking backwards retracts the bright portion. Pausing, stepping, portrait framing
and fullscreen keep the chart with the picture. It follows displayed frames rather than advancing
independently on the audio clock. These are preview graphics and are never baked into media or sends.

The graph title gives its units and range; hover the information symbol for source/timing details.
Capture graphs use sampled preview-frame positions; timing and IMU ranges keep grouped extrema.
Gyro uses recorded device axes. RAW gyro aligns through the receive clock and saved camera/profile
sync offset at mid-exposure/readout. This overlay does not run a visual sync fit; the tooltip says so.
Unavailable clock links or missing measurements are reported instead of inventing alignment. LOG
telemetry overlays use their recorded video clock. The RAW source-to-preview mapping retains repeat
frames and frame-rate conversion. A changed timing recipe reloads the chart.

The desktop keeps native playback: chart data is uploaded once per selection/change, and two small
cached chart strips are composited at the displayed frame. Ordinary browsers use SVG.

### Capture analytics

Open **Clip info** and its **Analytics** section on a RAW or LOG clip. Its metadata scan runs in the background with progress,
continues through playback/navigation, and caches results for the unchanged recording. RAW scans
read every JSON frame record by indexed seeks; regular LOG MP4/MOV files use indexed presentation
times. Both skip compressed video payloads. See the Library tab description above for the complete
graph list, on-demand lens-shading maps and full per-frame CSV export.

**Zoom timing variation** expands small timing differences. Timing groups preserve extrema from
every interval; missing/short-frame counts are timestamp-based estimates. RAW metadata graphs use
recorded frame numbers before trimming/CFR conversion. Four black levels retain their recorded
2x2 Bayer-site order; shading/noise plots retain their four separate colour channels.

Gyro and acceleration keep their recorded device axes and their own IMU time origin. They are not
silently synchronised to frame-index graphs. Unavailable LOG sensor coefficients are omitted rather
than inferred from developed pixels. Existing LOG previews and gyro processing use the original
video file without a virtual mount.

## What the recording's own data tells you

MCRAWsome reads every frame's metadata, the gyro and accelerometer log and the sound to describe each
recording, without decoding pictures. These are measurements and suggestions; none of them judges a
shot, and an intended choice (a long shutter, a quiet scene, a focus pull) stays valid.

**Search** matches file names and each clip's own details: title, description, keywords, notes,
scene, shot, take, location and camera label.

**Movement** comes from the gyro log: each second is **Still**, **Pan or tilt** (a steady turn in one
direction), **Moving** (strong up-and-down movement, as when carried while walking or travelling) or
**Handheld**, and a clip takes the movement that covers most of it. It appears in Info and as a
Library filter. **Shake blur** is the shake (the turning left over once its half-second average, the
movement a stabiliser follows, is removed) swept during each exposure, in pixels: a steady pan's own
blur is not counted. Info gives the blur most frames stay under, Analytics and the preview overlay
show it frame by frame, and Resolve Settings notes under Stabilise when a clip has sustained blur,
because steadying the picture leaves the blur visible. Direct Log videos record no shutter, so their
blur assumes a 180-degree shutter.

**Suggest trim**, above the Resolve Settings, sets the In and Out marks past the jolt of pressing record,
before the jolt of pressing stop and after focus settles, and says what moved. At most two seconds
go from either end and at least half of the clip stays. With several clips selected, **Suggest trims**
in the same place does the same for each clip that has a gyro log and no marks yet.

Library thumbnails show the steadiest frame of the middle half of each clip with a gyro log.

**Readiness** in Info adds what went wrong while recording, and why, once the clip is read:

- a frame rate the phone lowered because the shutter filled the whole frame time (for example
  23.79 fps from a 1/24 s shutter at 24 fps), with a shutter that keeps the rate;
- automatic exposure changing during the clip, with a pointer to Normalise exposure;
- automatic white balance moving, in the Kelvin Resolve receives (RAW pixels are unaffected, and
  mounts use the first frame's value);
- focus reversing direction often, as the Frequent focus changes review marker finds it;
- sound that clipped, came within half a decibel of clipping, is very quiet, has a silent channel, or
  has two identical channels.

**Place**: a clip with a GPS fix shows the nearest city within 50 km, found offline in the GNOME
location data installed with the system; nothing is sent anywhere. Notes offers it as the Location.

The **Shoot** filter groups clips recorded within two hours of each other (and within 5 km, when
both have a GPS fix); with a shoot chosen, **Sessions** offers **New session from this shoot**. The
**Place** filter groups clips by their nearest city. **Recorded together** groups clips whose
recordings overlap in time: one phone records one clip at a time, so these come from different
phones and, with time-of-day timecode, can be synced in Resolve by timecode.

**Lists** in the selection toolbar saves a camera report of the selected clips as CSV or Avid Log
Exchange (ALE): name, date, timecode in and out as their mounts would carry them, duration, lens,
exposure, white balance, movement, scene, shot, take, rating, location, GPS and notes. **Locations
(GPX)** saves a waypoint for each selected clip with a GPS fix, for maps and photo tools.

## Prepare a clip for Resolve

Select a clip and open **Resolve Settings** beneath the video. Drag the bar between the video and
the settings to share the pane between them (double-click it to restore the default); the settings
scroll beneath the video, which stays in view. Changes save automatically after a short pause. Mount and Send wait for unsaved edits; an error stops sending instead of using old settings.
**Reset** removes clip overrides and restores inherited defaults. Apply to Selected and Set as Default
copy Resolve-related adjustments while preserving each target's trim and output settings; exposure
adjustments are excluded.

For RAW, **Orientation**, **Stabilise**, rolling shutter compensation and optional **Horizon Lock**
remain. Horizon Lock and native stabilisation apply to RAW Timeline sends, require suitable sensors
and are constrained by the selected crop. The displayed source preview is neutral and unstabilised.

**Proxy**, **White / Black Levels**, **Mounted frame rate**, **Bake in Vignette Correction** and
**Reduce to Colour Correction** are visible without opening calibration. The last switch retains the
vignette while correcting only colour shading. Open **Advanced RAW settings** for individual black
and white levels, inherited resets, shading mode/strength, exposure normalisation, log transfer and
bit reduction, crop and camera-model options. These controls still use the existing mount engine.

For LOG, both input colour menus default to **Auto (file name or metadata)**. Verify the inferred
MotionCam profile after renaming files. A saved explicit choice takes priority. ARRI exposure index
selects a LogC3 curve variant, rather than changing exposure. Frame-rate choices retain the existing
recorded-timestamp mapping for preview; native RAW mounts and LOG conversions are separate workflows.

The timeline sits along the bottom edge of the picture. Hover or focus it to reveal In and Out grips.
The seek rail stays below the grips, including when a mark and the playhead share a position. Drag a
grip or use its arrow keys; I/O marks at the playhead remain. The same controls work in fullscreen.
While the grips show, the clip's sound runs between them as a waveform, louder parts taller (on a decibel
scale, so quiet sound still shows) and the marked part in the accent colour, to help find a clap or the
start of speech. It is the sound the preview plays.
The overlay menu and zoom stay above the video. No title or before/after controls cover the picture.

Play, pause, frame stepping, jumps to marks, Replay, volume and mute are retained. LOG reuses a
lane-owned decoder and scales before colour conversion. Verified VAAPI decoding/scaling is preferred
for previews on supported Intel/AMD hardware; an unsupported profile or scaling path falls back to CPU.
Where no VAAPI device passes, an NVIDIA GPU decodes with NVDEC once its trial passes in the same way, and
the processor scales its frames. It needs NVIDIA's own driver, which provides the CUDA and NVDEC
libraries. An older GPU without 10-bit H.265 or AV1 decoding fails the trial, and the processor decodes
instead. NVIDIA decoding has not been tried on NVIDIA hardware yet: `mcrawsome doctor` shows whether its
trial passed. Setting `MCRAWSOME_VAAPI_DECODE=0` disables GPU decoding of both kinds. Unchanged export
decoding still needs explicit opt-in. AV1 on the GPU uses FFmpeg's native AV1 decoder rather than the
software-only dav1d decoder.

Adaptive Quad Bayer previews average each same-colour 2x2 cell into an ordinary Bayer site before
reduced development. Full-resolution Source views, mounted DNGs and exports retain the full remosaic.
Explicit remosaic-off keeps recorded photosites. Ordinary Bayer footage is unchanged. RAW sound is
served as a seekable WAV directly from recording chunks; playback need not extract the whole track.

**Settings > Debug > Show preview performance statistics** reveals playback fps, frame processing
time, dimensions and backend information. It is off by default and saves across restarts. Recorded
FPS on Library cards remains visible. Performance depends on resolution, codec, hardware and load;
Adaptive may skip pictures to follow the sound, and a slow decoder keeps the newest completed picture
moving instead of discarding every late picture.

## Send to DaVinci Resolve

On Linux, the free edition of Resolve cannot decode the usual H.264/H.265 and AAC editing inputs.
Native Send prepares virtual CinemaDNG and aligned WAV audio for RAW recordings. Resolve cannot
import `.mcraw` files directly. Physical editing files remain available through the CLI.

### Send a RAW clip with a native archive

Select the RAW clips and use **Send to Resolve** in the card's three-dot or right-click menu.
**Media Bin** authors a `.drb`; **Timeline** authors a `.drt`, with computed corrections for clips
whose saved Stabilise settings and gyro data request them. Complete the normal
archive import in Resolve. The native archive retains virtual CinemaDNG RAW and paired WAV audio.
Keep the recordings and virtual mounts available while editing. Saved quarter-turn orientation uses
Clip Attributes. The earlier stabilisation section describes settings and limitations.

Native sends are available directly from Library. Old Automatic, Studio, Menu and Off preferences
migrate to native sending; the retired enable switch cannot leave a hidden restriction.
The physical **Export to Resolve** dialogue, scripted send-after-export, waiting-list routes,
and script installer have been removed. The current `send-to-resolve` CLI uses the same native archive
workflow as the file-manager menu. No Lua or external Resolve
scripting is used. Files from an older script installation are left untouched and are not run by
MCRAWsome. For independent CinemaDNG folders, use the command-line CinemaDNG export, or
`mcrawsome export CLIP.mcraw --to dng`, and import the folder into your editor manually.

### Time-of-day timecode

**Settings > DaVinci Resolve > Timecode** chooses how mounted media is numbered. **From zero** (the
default) starts every clip at 00:00:00:00. **Time of day** starts each clip at the time its first
frame was recorded, on the phone's clock: every mounted DNG carries its own CinemaDNG timecode and
the WAV a Broadcast Wave time reference with the same start, so Resolve can sync clips from several
phones, or an audio recorder set to the same time, by timecode. The capture time comes from each
frame's receive time on the phone, corrected by the camera profile's sync offset, so it is as
accurate as the phone's clock (network time keeps phones within a frame or two of each other).
Rates above 30 fps count pairs of frames per timecode frame, as SMPTE timecode does.

**Time zone** chooses this computer's time zone or, for clips with a GPS fix, the zone where they
were recorded (offline, from the nearest city). The choice applies to media mounted from then on;
media already mounted keeps its timecode, so open Resolve projects are unaffected. The command line
does the same for exports with `--output.timecode time-of-day` (and `--output.timecode_zone`).
Native bin and timeline archives keep the start fields measured in Resolve; check in Resolve that a
native send shows the time of day before relying on it, or import the mounted folder directly.

### Measured keywords

**Settings > DaVinci Resolve > Add measured keywords** adds each clip's movement (Still, Pan or tilt,
Moving or Handheld), and Motion blur when it has a second or more of it, to the keywords Resolve
receives, after your own keywords. Smart Bins can then gather clips by movement. Your saved keywords
are not changed.

## Use a clip in Blender (command line)

MCRAWsome has no Send to Blender. Export the clip as OpenEXR instead: a folder of half-float frames in
scene linear (ACEScg unless you choose Linear Rec.709) with the clip's development and exposure, and
a WAV aligned to the first frame. Looks, film effects and stabilisation are not applied; grade in
Blender.

```sh
mcrawsome export CLIP.mcraw --to exr                              # ACEScg
mcrawsome export CLIP.mcraw --to exr --output.exr_space linear-rec709
```

In Blender's Video Sequencer:

1. Set the scene's frame rate to the clip's (Output Properties > Format; for a rate that is not whole,
   choose **Custom** and set **FPS** and **Base** so FPS divided by Base is the clip's rate) and its
   resolution to the frames' size.
2. **Add > Image/Sequence**, open the folder, select every frame (**A**) and add them. In the strip's
   **Source** panel set **Color Space** to **ACEScg** (or **Linear Rec.709**, as exported).
3. **Add > Sound** with the folder's WAV, starting at the same frame as the image strip.
4. Optionally set **Color Management > Sequencer** to **Linear Rec.709** so strips blend in linear
   light.

Half-float 4K frames take tens of megabytes each, so a minute of video needs tens of gigabytes, and
Blender plays long EXR sequences slowly; use **Proxy** in the Sequencer's strip settings, or trim the
clip before exporting. For VFX work the same folder serves as plates in the Compositor or Movie Clip
Editor.

## Where settings and work are stored

For `CLIP.mcraw`, Resolve Settings writes `CLIP.mcrawsome.toml` beside the recording. When that folder is
read-only and there is no existing adjacent layer, it writes to MCRAWsome's state/data storage under
`~/.local/state/mcrawsome/clip-recipes/` instead. The fallback is tied to the clip's path, size and
fingerprint; moving it can change which fallback is found. An existing adjacent settings file takes
precedence, so an unwritable existing file can still cause a save error.

The CLI reads the same clip layer automatically. Settings resolve in this order, with later values
winning: built-in defaults, `~/.config/mcrawsome/defaults.toml`, the folder's `mcrawsome.toml`, the clip's
layer, then explicit Recipe files and command overrides. For example:

```sh
mcrawsome export CLIP.mcraw --dry-run
```

shows the Recipe the CLI would use with your saved edits. Each completed export also gets an output `.mcrawsome.toml` sidecar
recording its full Recipe, measurements and decisions. Keep this output record separate from the
input clip's editable settings file.

Interface preferences live in `~/.config/mcrawsome/interface.toml`. The state folder normally holds
logs, session snapshots, the library index, native Resolve archives and timing history at `~/.local/state/mcrawsome`.
`XDG_CONFIG_HOME` moves configuration; `XDG_STATE_HOME` moves state, and `MCRAWSOME_STATE_DIR` overrides
that state location. Cache files normally live in `~/.cache/mcrawsome`, moved by `XDG_CACHE_HOME` or
`MCRAWSOME_CACHE_DIR`.

## The background engine

Closing the native window leaves independent mounted-media services available to the editor.
**Keep the engine running in the background** under **Settings > General** keeps the engine available
as well. A window attached to an existing engine does not stop it; browser mode leaves it running.
A bar across the top of the window appears only when the engine has stopped, or after a few seconds without its connection, and offers **Restart** in the native window when it has stopped.

## Settings and About

Settings saves changes as you make them, with at most two columns and one on narrower windows.
**General** contains Library folders, the mount folder and the background engine switch.
**DaVinci Resolve** contains edition and send preferences. **Use DaVinci Resolve Studio** is off by default: Free uses
compatible timeline dimensions with a warning when reduced; Studio keeps the source resolution.
**Separate video and audio bin items** is off by default, so each recording appears as one media
item containing both. Turn it on for separate video and WAV items. **Keep separate video
and audio linked** is indented beneath it and initially on, so the pair moves together onto a timeline.
The link toggle is greyed out while separation is off. Turn linking off for
independent media. The link preference is remembered while combined media is selected; combined
media always carries its matching audio. These preferences affect new Media Bin and Timeline sends.
The Debian package supplies the file-manager actions and `.mcraw` association. Your file manager
controls the default application for those files.
**Processing** contains **Quad Bayer Remosaic**, the preview GPU switch and the scrub cache.
**Debug** contains **Show preview performance statistics**, off by default. Export destination and
render-worker preferences are no longer shown in the interface.

Hover an **(i)** icon for the option's explanation. Keyboard focus and a tap also show the information;
**Escape** dismisses it. Save errors stay beside the relevant control. If saved interface settings
cannot be read, Settings says it is showing defaults; the unreadable file stays untouched until you
change a setting there.

Open **About** for **Version**, **System check** and **Components and licences**. System check
lists `ok`, `warn` or `fail`, details and suggested fixes. Expand a component to read its licence.
The licences listed are those of MCRAWsome and the Python packages installed with it; Debian's own
packages keep theirs in `/usr/share/doc`.

## Troubleshooting

- **A folder or clip is missing:** check the saved Folders list, reconnect the drive and wait for
  copying to finish. Only the folder and one subfolder level are scanned. An unreadable card explains
  why its input cannot be opened.
- **The preview cannot play:** try Previous frame, Next frame or scrubbing. Read any preview error.
  Audio playback is needed for Play, and preview speed can be below real time.
- **Settings were not saved:** wait for Saved, read the error, and check folder/file permissions.
  Changes made elsewhere reload automatically; inspect the current controls before editing again.
- **The engine stops or a tool is missing:** run `mcrawsome doctor` in a terminal. For a source checkout,
  use `uv run mcrawsome doctor`. `mcrawsome doctor --refresh` repeats cached GPU encoder probes after
  a driver or FFmpeg change. A warning usually concerns an optional feature; a failure needs attention.
- **GPU decoding of Direct Log videos:** verified VAAPI decoding/scaling is preferred on supported
  hardware, then an NVIDIA GPU's NVDEC. `mcrawsome doctor` gives each GPU's trial result (**decoder gpu
  hevc** and **decoder gpu av1**). Set `MCRAWSOME_VAAPI_DECODE=0` to use CPU decoding for comparison.
- **An export or analysis stops with "A worker process stopped unexpectedly":** one of its worker
  processes ended mid-task, so it stops rather than wait for a frame that will never arrive. SIGKILL
  usually means memory ran out: close other programs, then try again.
- **A send fails:** read its error and inspect the engine log at
  `~/.local/state/mcrawsome/server.log`. A manually started server prints to its terminal.
- **Resolve import:** complete the native archive import, and keep its virtual media mounted.

For a bug report, open **About → Help and support → Collect support report**. Collection runs only
when requested. Review the report, then save JSON using the desktop file dialogue or a browser
download, or copy it. If the clipboard is unavailable, selectable text is provided. The report
contains system checks, dependency versions and available current/previous
engine logs, capped at 64 KiB per member. Home-folder paths become `~`; file and folder names remain.
It does not read selected recordings or collect clip metadata, sidecars, pictures, sound or gyro samples.
Nothing is sent automatically. A manually started engine may log only to its terminal.

For a larger ZIP report, run `mcrawsome support-bundle`, optionally followed by a clip path and its output
sidecar. It creates a ZIP with system checks and supplied metadata/settings, without
picture, sound, gyro samples or recording location. File and folder names remain, so inspect it before
sharing. See [Bug reports](../README.md#bug-reports) for the command and its options.


### RAW level sources

Resolve Settings offers separate **Recorded per frame**, **Static (frame 1 for every frame)** and
**Use inherited levels** choices. Recorded per frame bypasses fixed black/white values saved in a
folder or application default. Static uses the recording's first frame, including for trimmed clips.
Named presets and custom values select explicit levels. Existing saved presets/snapshots keep their
values; they are not reinterpreted as the new static policy. The source text distinguishes this clip,
folder, application and recording. Use inherited removes the clip's policy and level overrides.

**Recording camera** similarly bypasses an inherited mounted-camera name. Use inherited camera
restores the folder/application choice. Adding the clip name remains a separate setting. These
choices carry through Apply to Selected, Set as Default and TOML save/reload. Sending changed RAW settings creates
a new immutable mount; media already held by Resolve keeps its saved settings.


### Cache management and scoped resets

Settings → **Cache management** shows completed disk-cache file counts, byte usage and existing limits.
Analysis reports, optical tracking and exposure measurements can be cleared independently. Active
mounts retain their own data. Playback audio, thumbnail responses and session plans remain protected;
this control never recursively deletes the cache tree or touches recordings, sidecars, profiles,
sessions or mounts. New measurements may repopulate the caches while work continues.

Settings → **Reset settings** has independent defaults, preferences and workspace actions. Read each
scope description before using its button. Defaults resets application adjustments and preserves
folder/clip overrides. Preferences resets interface choices and watched folders.
Workspace starts an empty active workspace while retaining named session files and recent history.
Each action first saves a persistent backup. **Restore previous …** restores it if no newer edits
have been made; backups remain available after reopening reset options and under the displayed
backup path. A failed reset preserves the original settings. Neither reset nor restore changes
recordings, sidecars, profiles, mount records or server configuration.


### Sensor clipping preview

In Resolve Settings, **Sensor clipping** offers **Pink: any sensor channel** and **Pink: all RGB
channels**. Pink means recorded photosites at or above the frame's recorded white code, before
shading, white balance or remosaic. Either green site counts as green. This diagnostic is independent
of custom black/white overrides and does not report clipping introduced by later corrections. It
uses original 4×4 cells for Quad Bayer. The mask follows the displayed frame, crop and rotation and
retains isolated clipped cells when reduced. It is shown while paused and hidden during playback.
The selector is not saved and never changes mounted media or Resolve archives.


### Custom mounted RAW crop

Choose **Custom rectangle** under Mounted DNG crop. The preview shows the whole recorded frame with
the crop on it: drag inside the rectangle to move it, or drag an edge or corner to resize it. **Shape**
holds a ratio (Recorded, 16:9, 17:9, 2.39:1, 4:3, 1:1, 4:5 or 9:16) while resizing, and the width,
height and position fields set exact values in pixels of the picture as shown. **Centre** and **Whole
frame** help placement; **Apply crop** saves it and **Cancel** leaves the saved crop unchanged. Every
edge snaps to 2 sensor pixels at full size, 4 with a half-size proxy, 8 at quarter size or 16 at
eighth size, so the colour pattern and proxies stay aligned. The crop is saved in recorded sensor
pixels, before orientation, as `WxH+L+T` (or `WxH` from the top-left, as earlier versions saved it).
Preview framing follows the same rectangle. Sensor calibration, orientation, frame timing, trim and
audio alignment are retained; existing mounted media keeps its saved crop. Cropped gyro timelines
remain unsupported until their full projection and rolling-shutter geometry is validated.
