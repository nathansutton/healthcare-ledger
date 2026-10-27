"""What every source shares: identifier quoting and types (``sql``), connection settings (``connect``),
the CSV reader fallbacks (``csvread``), column declarations (``columns``), the row and version hashing
and content fingerprints (``versioning``), the SCD Type 2 load (``engine``), the privacy rules and the
audit of exported files (``privacy``), and the release machinery that spans sources (``export``,
``release``, ``validate``, ``package``, ``filesources``). A new source uses these rather than its own copies."""
