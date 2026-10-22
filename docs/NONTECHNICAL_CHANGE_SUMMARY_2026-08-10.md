# Serv camera update — nontechnical summary

We combined Theo's newest plate-reading improvements with the new system that
counts every vehicle, including cars whose plates cannot be read. The design
now keeps two ideas separate: a real car passing the camera is a **visit**,
while a weak or failed plate reading is a **diagnostic**. That prevents failed
frames from being mistaken for extra cars.

Theo's latest follow-up is included too. When the system explains a weak read,
its detection and reading scores now come from the same real video frame; it
no longer combines the best detection from one moment with the best text read
from another. AWS stores only the specifically approved numeric counters and
random episode ID—never raw plate text or arbitrary device-supplied fields.

The biggest security improvement is that pushing code to GitHub can no longer
automatically give that code administrator control of every camera. Each exact
release must be reviewed and approved on the camera, and the camera reports
both the approved version and a fingerprint of the code actually running.

We also removed raw plate text from diagnostic logs, locked down the local
database and camera previews, separated writable data from protected program
code, removed the camera program's ability to use `sudo`, and built a firewall
that accepts remote administration only through the private Tailscale network.

The field Pi is healthy: the real camera and licensed recognition engine came
back successfully after a full software and kernel update. A ten-minute soak
stayed between 56.0 and 60.4 C with no throttling or power warnings. It still
needs passive cooling before it is sealed in its final enclosure.

The network is now default-deny, unnecessary services are disabled, software
updates are fully installed, future routine security updates are enabled
without automatic reboot, and the unsafe updater was replaced. We also
found and removed a passwordless local-console login that would have allowed
someone with physical keyboard/display access into the camera. The two
approved SSH keys belong to Theo and Carlos, and no failed or unknown recent
SSH login appeared in the retained journal.

No historical records were deleted. Twenty previously rejected diagnostics
were safely delivered after AWS began accepting the new categories. AWS now
contains exactly 16 low-score and 4 grouped-confidence records. Nine older
`vehicle_only` records will stay preserved but will not be replayed because
they could count some cars twice.

Software cannot stop physical vandalism. Field units still need a tamper-
resistant enclosure and fasteners, concealed or armored PoE cabling, surge
protection, placement outside easy reach, and ideally a second camera watching
the equipment. Missing-heartbeat and camera-health alerts provide the software
side of that protection.

The remaining security work needs Carlos: unlock the Mac SSH key once so the
known weak Pi password can be disabled, and rotate the exposed per-camera AWS
API key through a secret-safe channel. The remaining field work is installing
cooling and the tamper-resistant enclosure, defining the real drive-through
zones, and running the labeled 30-car recording through the complete system.
