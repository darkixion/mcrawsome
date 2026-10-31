/* Debian's release WebKit ignores WEBKIT_EXEC_PATH. Keep its helpers in the AppImage.
 * SPDX-License-Identifier: Apache-2.0
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <gio/gio.h>
#include <string.h>

typedef GSubprocess *(*Spawn)(GSubprocessLauncher *, const gchar *const *, GError **);

GSubprocess *g_subprocess_launcher_spawnv(GSubprocessLauncher *launcher,
                                        const gchar *const *argv, GError **error)
{
    Spawn original = (Spawn)dlsym(RTLD_NEXT, "g_subprocess_launcher_spawnv");
    if (!original) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_FAILED, "Cannot find GLib's subprocess launcher");
        return NULL;
    }
    const gchar *directory = g_getenv("MCRAWSOME_WEBKIT_HELPERS");
    if (!directory || !argv)
        return original(launcher, argv, error);

    const gchar *prefix = "/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1/";
    gboolean changed = FALSE;
    GPtrArray *mapped = g_ptr_array_new_with_free_func(g_free);
    for (gsize i = 0; argv[i]; ++i) {
        const gchar *name = g_str_has_prefix(argv[i], prefix) ? argv[i] + strlen(prefix) : NULL;
        if (name && (!strcmp(name, "WebKitWebProcess") || !strcmp(name, "WebKitNetworkProcess") ||
                     !strcmp(name, "WebKitGPUProcess"))) {
            g_ptr_array_add(mapped, g_build_filename(directory, name, NULL));
            changed = TRUE;
        } else {
            g_ptr_array_add(mapped, g_strdup(argv[i]));
        }
    }
    /* The sandbox must see the same read-only libraries and helper path as its parent. */
    const gchar *appdir = g_getenv("APPDIR");
    if (changed && appdir && g_str_has_suffix(argv[0], "/bwrap")) {
        g_ptr_array_insert(mapped, 1, g_strdup("--ro-bind"));
        g_ptr_array_insert(mapped, 2, g_strdup(appdir));
        g_ptr_array_insert(mapped, 3, g_strdup(appdir));
    }
    g_ptr_array_add(mapped, NULL);
    GSubprocess *process = original(launcher, (const gchar *const *)mapped->pdata, error);
    g_ptr_array_unref(mapped);
    return process;
}
