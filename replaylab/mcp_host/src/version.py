"""Single source of truth for ReplayLab application protocol metadata."""

SERVICE_VERSION = "1.46.142"
HOST_STATE_PROTOCOL_VERSION = 2
INTERNAL_HOST_STATE_PROTOCOL_VERSION = 1

# The private qrenderdoc bridge and ReplayLabInternal Host evolve together, but
# their package versions alone do not describe the individual wire contracts a
# caller needs.  Publish the exact schema/capability versions before the bridge
# reads the bearer token or submits a restore job.  Additive Host capabilities
# may be omitted by older clients; every entry listed here is required by the
# bundled bridge at this release.
INTERNAL_HOST_CAPABILITY_SCHEMA = "replaylab-internal-host-capabilities/v1"
INTERNAL_HOST_CAPABILITY_VERSIONS = {
    "project-service": 1,
    "project-restore": 1,
    "shader-workspace-prepare": 1,
    "shader-source-save": 1,
    "shader-live-edit": 1,
    "asset-index": 1,
}
