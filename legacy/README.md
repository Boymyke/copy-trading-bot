# Legacy scripts — do not run

These are earlier versions of the copier (local MT5 terminal scripts and the
v1–v3 MetaApi workers). **Several of them send real orders** (open, close and
SL modifications). They are kept only for reference.

Railway runs `python -m app.main`, which does not import anything from this
folder. The current architecture is described in the top-level `README.md`.
