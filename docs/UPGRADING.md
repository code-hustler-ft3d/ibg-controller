# Upgrading

Short reference for moving an existing `ibg-controller` deployment
from one version to the next. The [CHANGELOG](../CHANGELOG.md) is the
authoritative list of what changed; this file is the operator-facing
how-to.

## Version scheme

`ibg-controller` is pre-1.0 and follows [SemVer](https://semver.org/)
with the pre-1.0 caveat: **minor bumps in the `0.x` series are
allowed to contain breaking changes**. Every release that does
contain one calls it out in the **Removed** or **Changed** sections
of the CHANGELOG, and in this file under the corresponding version.

What's covered by the stability contract regardless of version:

- Names and key structure of `ALERT_*` log tokens
  ([`OBSERVABILITY.md`](OBSERVABILITY.md)).
- Field names and semantics of the `/health` JSON shape.
- Env var names listed in the README's env table.

Changes to any of those within `0.x` will still be called out — the
contract is "we won't break it *silently*", not "we won't break it".

## The upgrade workflow

Same workflow regardless of how you deployed:

### Docker image (built locally from source)

```bash
cd ibg-controller
git fetch --tags
git checkout vX.Y.Z                    # the tag you want
make                                    # rebuild the agent jar + stage controller
docker build -t ibg-controller:vX.Y.Z .
docker rm -f ibkr && docker run -d \
  --name ibkr \
  --env-file /path/to/.env \
  -p 127.0.0.1:4001:4001 \
  -p 127.0.0.1:8080:8080 \
  ibg-controller:vX.Y.Z
```

### Release tarball (prebuilt)

```bash
VER=X.Y.Z
curl -sSLO https://github.com/code-hustler-ft3d/ibg-controller/releases/download/v${VER}/ibg-controller-${VER}.tar.gz
tar -xzf ibg-controller-${VER}.tar.gz
cd ibg-controller-${VER}
DESTDIR=/home/ibgateway ./install.sh
# Restart the Gateway container so the new controller + agent are picked up
docker restart ibkr
```

### Rollback

`ibg-controller` keeps no on-disk state besides the readiness file
`/tmp/gateway_ready*`, so rollback is just "redeploy the previous
version":

```bash
git checkout v<previous>
make && docker build -t ibg-controller:v<previous> . && docker rm -f ibkr && docker run ...
```

Your `.env` does not need to change on rollback. New env vars
introduced in the release you're rolling back *from* get ignored
when they're absent; env vars you already set stay honored.

## Per-version notes

Only versions that need operator attention are listed. If a version
isn't listed, it contained only additive changes that don't require
anything from you.

### v0.5.2

**No breaking changes.** Additive:

- New `ALERT_SHUTDOWN` log token (INFO-level) emitted on SIGTERM /
  SIGINT. Optional to wire up — it helps distinguish
  operator-initiated restarts from JVM crashes in dashboards. See
  [`OBSERVABILITY.md`](OBSERVABILITY.md#alert_shutdown) for the
  recommended threshold on `graceful=false` occurrences.
- New [`FROM_IBC.md` unsupported-IBC-keys matrix](FROM_IBC.md#unsupported-ibc-keys)
  for users on IBC evaluating a switch.

### v0.5.1

**No breaking changes.** Bug fix + new alert token:

- `BYPASS_WARNING` is now honored in both dismissal code paths (was
  only honored in the opportunistic post-login sweep before). If you
  had `BYPASS_WARNING` set and were seeing post-login disclaimers
  *still* block, v0.5.1 fixes that. No action required.
- New `ALERT_LOGIN_FAILED` token. Wire your alerting on it to catch a
  rotated-password-not-yet-mirrored-into-env scenario before the CCP
  streak escalates and IBKR locks the account.

### v0.5.0

**No breaking changes.** New tooling + alert:

- `scripts/ibc_config_to_env.py` one-shot migration tool for users
  coming from IBC. Also in the release tarball at the root.
- `ALERT_PASSWORD_EXPIRED` token. Surfaces the IBKR password-rotation
  warning dialog (with `days_remaining=N` when available) and the
  login-blocking expired variant. Wire alerting on this — IBC doesn't
  surface it this cleanly.

### v0.4.0

**Breaking behaviour change worth knowing about.** Auth-recovery
paths no longer invoke `do_restart_in_place` on credential failures.
Instead, a new in-JVM relogin sequence matches IBC's
`LoginManager.initiateLogin` semantics, staying in one JVM. This
avoids feeding IBKR's CCP rate limiter during retry loops. If you
had monitoring counting JVM restarts as a liveness signal, note that
a healthy running-but-reauthing controller will now show fewer JVM
restarts than previously.

## Watch for in your logs after an upgrade

First 30 minutes on a new version:

```bash
docker logs -f ibkr 2>&1 | grep -E 'ALERT_|ERROR|CRITICAL'
```

First successful login cycle confirms the upgrade is healthy:

```bash
curl -sf http://ibkr:8080/health | jq
# Expect: "status":"healthy", "state":"MONITORING", and
# the "version" field reflects the new release.
```

If `/health` reports the old version number, the image rebuild
didn't pick up the new controller — check your Dockerfile's `COPY`
step and rebuild without cache (`docker build --no-cache`).
