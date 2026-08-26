# dev-sboms

Sample CycloneDX SBOMs for the local `docker compose` stack. Dev/test only —
never shipped in the image, never uploaded to the real MinIO.

`scripts/seed_minio.py` uploads **every `*.json` in this folder** into the
compose-local MinIO bucket, so:

```bash
syft nginx:1.27-alpine --select-catalogers "-file" \
  -o cyclonedx-json=dev-sboms/my-image.cdx.json
docker compose down -v && docker compose up --build
```

is all it takes to browse your own image. No code change needed.

The committed files are real `syft` output from three Alpine-based images.
They overlap deliberately: `libssl3`, `libcrypto3`, `musl` and `busybox`
appear in all of them at *different versions*, which is the question KaBOM
exists to answer — search `libssl3` and you get every image that has it,
each with its own version and its own data age.

`--select-catalogers "-file"` skips Syft's per-file entries. They are ~95% of
the output by count, KaBOM only searches package names, and leaving them in
made these fixtures 3× larger for nothing.
