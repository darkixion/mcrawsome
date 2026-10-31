# Resolve marker reference

Synthetic two-marker payload exported from Resolve Studio 19.1.3.7, published by DaVinci Resolve MCP:
https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/resolve-advanced/test/fixtures-r19-markers-2.hex

Frame 24: Blue, MK_ALPHA, first note, duration 1. Frame 60: Red, MK_BETA, second note, duration 12.
The test compares decompressed native payload bytes, independently of zstd compressor settings.
Archive ownership uses a Sm2SequenceLockableBlob belonging to the sequence, rather than MarkersBA.
This reference does not replace acceptance of a generated archive in the owner's Resolve 21.1.

MIT licence and copyright: packaging/LICENCES/DaVinci-Resolve-MCP.txt; attribution in NOTICE.
