Context: This is a two-repo project. The Next.js frontend (botchain-ai-next-app)
is already underway — Kinde auth is wired up, and the Prisma + Neon Postgres
database is live with the User, Chat, Message, Attachment, CreditWallet,
CreditTransaction, and PaymentTopup tables created. UI work on the
/users/:userId routes is in progress there.

This repo (botchain-ai) is the Python/FastAPI backend. It currently only has
a prototype (prototype.py) and planning docs — no production API yet. Your
job now is to bring this repo to production-ready shape. It will later be
connected to the Next.js app via streaming FastAPI routes, but that wiring
happens after this backend itself is in good shape — don't try to build both
sides at once.

Before writing any code, do these in order:

1. Read everything in `_markdown/` at the repo root first. It contains two
   files — `python-fastapi-backendchecklist.md` and `backend-setup-qa.md` —
   that define the phases, milestones, and already-settled architecture
   decisions for this backend. This is the source of truth for what to build
   and in what order. Don't invent your own sequencing or skip ahead to a
   later phase before an earlier one is actually working.

2. Read everything in `_nextjs_repo_context/` at the repo root before
   touching anything database-related. It holds reference files from the
   Next.js repo, including `contract.prisma` — the actual live schema of the
   Neon Postgres database this backend will connect to. Those tables already
   exist in production. Your SQLAlchemy models must match that schema
   exactly — same table names, same columns. Do not rename, add, or drop
   columns on tables Prisma owns (especially `users`) without flagging it to
   me first; changing that side means a change on the Next.js repo too, not
   just here.

3. Note that the folder structure this backend should use is now spelled out
   inside `python-fastapi-backendchecklist.md` (I updated it to include this).
   Follow that structure exactly when scaffolding the repo, and keep it
   consistent as you add files — don't drift into a different layout partway
   through.

Work through the checklist phase by phase, confirming each phase actually
works before moving to the next. If anything in the checklist is ambiguous,
or your implementation needs to diverge from it for a good reason, stop and
tell me before proceeding rather than deciding silently.