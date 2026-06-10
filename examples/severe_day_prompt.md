# Example agent prompt: multi-tool severe-day reasoning

A prompt that exercises SHEARLINE the way a desk analyst works — outlook first,
then environment, then real-time products, then a synthesized call. Paste it
into any agent with SHEARLINE connected and substitute your location.

---

I'm coordinating an outdoor event in Norman, Oklahoma (35.22, -97.44) that runs
from 4 PM to 10 PM local time today. Work through the severe weather picture
like an analyst and tell me whether to put the contingency plan on standby:

1. Start with `get_spc_outlook` for today (day 1). What category are we in, and
   what are the tornado/hail/wind probabilities? If we're SLGT or higher, also
   check day 2 in case the event gets postponed.
2. Pull `get_point_environment`. Don't just read me numbers — tell me which
   parameter space this is (pulse? high-shear/low-CAPE? classic supercell?) and
   what hazard that regime favors. Pay attention to whether instability is
   capped (CIN) and when that might matter.
3. Check what's already happening: `get_active_warnings` (40 km),
   `get_mrms_severe` (40 km), and `get_storm_reports` for the last 3 hours out
   to 80 km. Are storms already producing hail or rotating anywhere upstream of
   us, given the storm motion from any active warnings?
4. Then call `get_threat_brief` and reconcile it with your own read from steps
   1-3. If the brief's threat level and your read disagree, say why.
5. Finish with a recommendation: green / standby / activate, the time window
   of greatest concern, and the single observation that would most change your
   mind (e.g. "a rotation track appearing within 60 km southwest").

Remember: this is planning support only — official NWS warnings drive the
actual evacuation decision.
