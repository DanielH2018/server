### PR C-2a and C-2b

#### PR C-2a

[#7086](https://github.com/DanielH2018/server/issues/7086) deletes `uptime-kuma` from the bespoke
policies and from ical-proxy's `netpol_from`.

**One PR holds the order.** `netpol-baseline` renders all five grants, including ical-proxy's fence
through `networkpolicy-callers.yaml.j2`. No workload role's render moves, so *the roles first* has
nothing to order.

**Three of the four bespoke policies held a grant.** `networkpolicy-jellyfin.yaml.j2` leaves `:8096`
open to every source and never named Kuma as a peer. It held only a comment about Kuma's VIP tile,
and that comment is what C-2a deletes there.

C-2b has not started.
