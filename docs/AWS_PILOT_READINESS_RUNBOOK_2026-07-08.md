# AWS Pilot Readiness Runbook

Date: 2026-07-08

## Scope

This runbook turns the Netcode control plane into a deployable AWS pilot service.
It does not claim AWS is already deployed. It defines the repo artifacts and
acceptance checks needed before moving from the Mac demo to AWS.

## Target Architecture

- Netcode control plane runs in ECS/Fargate or an equivalent container host.
- Rez backend runs as a separate service and calls Netcode through the bridge.
- ALB terminates TLS and WebSocket traffic.
- RDS Postgres stores Netcode state for pilot durability.
- EFS or equivalent persistent volume stores workspace artifacts.
- Windows/Linux runners live inside the customer network and make outbound-only
  HTTPS/WSS connections to the AWS endpoint.
- Device credentials remain on the runner.
- Rez Diagnostics uses read-only runner actions.
- Netcode writes require plan, dry-run/canary, human approval, apply, and verify.

## Artifacts Added

- `Dockerfile` builds the Netcode control-plane image.
- `deploy/aws/netcode.env.example` documents required env/secrets.
- `deploy/aws/docker-compose.pilot.yml` provides a local container smoke target
  matching the ECS runtime shape.

## Acceptance Checks

1. Build the image:

   ```bash
   docker build -t netcode-pilot .
   ```

2. Run the local pilot compose smoke:

   ```bash
   docker compose -f deploy/aws/docker-compose.pilot.yml up --build
   curl http://127.0.0.1:8095/api/health
   ```

3. Enroll an existing ORB/Linux runner against the container endpoint.

4. Download the Windows runner ZIP:

   ```bash
   curl -o netcode-windows-runner.zip http://127.0.0.1:8095/api/runner/download/windows
   ```

5. Confirm the backend has no direct route to lab devices, then prove:

   - discovery works through runner;
   - `rez_ssh_command` works through runner;
   - workflow-pack plan/dry-run/apply/verify works through runner;
   - Rez read-only diagnostics cannot call write actions.

## AWS Notes

- Use a single ECS task for the first pilot. The current control plane keeps
  runner/websocket/session state in-process.
- Put `NETCODE_REZ_BRIDGE_TOKEN`, bootstrap password, and database password in
  Secrets Manager.
- Expose only HTTPS/WSS through the ALB.
- Do not open any inbound network path from AWS to customer devices.

## Stop Conditions

- Any credential appears in a control-plane payload, response, log, or artifact.
- Any Rez endpoint queues a non-read runner job.
- Apply is possible without a successful dry-run and human approval.
- Runner requires inbound connectivity from AWS.
