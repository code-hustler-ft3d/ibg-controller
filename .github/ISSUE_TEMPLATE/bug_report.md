---
name: Bug report
about: Something isn't working as expected
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear description of what happened vs what you expected.

**Diagnostics** (please include all of these — they save a round-trip)

Controller log:
```
docker logs <container> 2>&1 | tail -100
```

Gateway launcher.log:
```
docker exec <container> cat /home/ibgateway/Jts/launcher.log | tail -50
```

Environment:
- Gateway version (`TWS_MAJOR_VRSN`): 
- Architecture: amd64 / arm64
- Trading mode: live / paper / both
- Container image: (your image name + tag)
- `USE_IBG_CONTROLLER`: yes  <!-- or USE_PYATSPI2_CONTROLLER if you're still on the deprecated alias -->

- Other relevant env vars (redact credentials!):

**Steps to reproduce**
1. `docker run ...` with these env vars
2. Wait for ...
3. See error at ...

**Before submitting**: scrub your logs of account numbers, usernames, passwords, and TOTP secrets. The controller redacts `DU*****` patterns but Gateway's own launcher.log may not.
