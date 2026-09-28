# Backend Phase 6.6 handoff — what the Next.js repo must build

Status: the **backend work for chat lifecycle + account purge is DONE and verified**
(repo `botchain-ai`, `src/app/`). This file tells agents working in
`botchain-ai-next-app` exactly what to build and how the backend will behave.
Companion docs: `_markdown/phase6.6/backend-lifecycle-plan.md` (backend, done) and
`_markdown/phase6.6/nextjs-lifecycle-plan.md` (frontend — read it; this file is the
authoritative override where noted).

## Locked decisions (already made by the owner — do not re-ask)

- **`KINDE_DELETE_MODE = local-only`.** No Kinde M2M app, no Management API, no
  `@kinde/management-api-js`. **Skip the frontend plan's 3a and the Kinde-delete step
  (3b step 5).** Deleting the account erases all app data + the local `users` row; the
  Kinde login remains and the next login recreates an empty account via
  `/api/auth/sync`.
- **Chat title cap = 120** (backend enforces strip + 1–120; mirror the input cap on the
  frontend). **Rename/Delete UI is list-page only** — skip the optional chat-view header
  menu (frontend plan step 2 "Optional").
- Everything else in the nextjs lifecycle plan stands (ownership rules, Prisma-for-users
  only, `revalidatePath` after mutations, workflows written only through the backend).

## Backend contract (live, verified)

| Call | Success | Errors to surface |
|---|---|---|
| `PATCH /api/v1/chats/{id}` `{title}` | 200 `ChatOut` | 404; 422 (title missing/blank/over 120 — show `detail`) |
| `DELETE /api/v1/chats/{id}` | 204 | 404 = already gone (treat as success); **409** = active run |
| `DELETE /api/v1/me/data` | 204 (idempotent) | 401; **409** = active run; 5xx |

Exact error strings the backend returns — **show these verbatim** (they come from
`BackendError.detail`):

- Delete chat 409: `A response is still being generated for this chat — try again in a moment.`
- Purge 409: `A request is still being processed for this account — try again in a moment.`
- A chat parked at **pending approval** holds no lock → it **is** deletable. Only an
  actively-streaming run blocks deletion.
- `DELETE /me/data` is idempotent: the frontend can fire it unconditionally and treat
  any 204 as success; it **never** touches the Kinde identity.

## Facts already verified in the Next.js repo (trust these, don't re-research)

- `lib/` and `components/` live at the **repo root** (`@/*` → `./`), not under `src/`.
- `lib/user.ts` line 1 is `'use server'` — that turns its exports into public Server
  Actions. Fix for the delete flow: convert it to a plain helper and add
  `import "server-only"` (**`server-only` is not installed — `pnpm add server-only`**).
  Grep that no client component imports it first.
- `lib/backend.ts` already has `fetchBackend` + `BackendError(status, detail)`; it
  requires a leading `/` on the path and uses `encodeURIComponent` for id segments.
- No `revalidatePath`, `useTransition`, or `useActionState` anywhere yet (house style is
  `useState`). Fine to introduce the action-state hooks for the delete dialog.
- shadcn components are **Base UI** (not Radix): children render via
  `render={<X/>}`, not `asChild`. `Dialog`, `AlertDialog`, `DropdownMenu`, and
  `CardAction` already exist.
- Chat list page: `src/app/users/[userId]/chats/page.tsx` — already filters
  `deletedAt === null`.
- Danger zone goes on the existing `src/app/users/[userId]/page.tsx` near the
  "View Chats" button (~line 54). No new route/URL.
- The user-facing id everywhere is the **local `User.id`**, never the Kinde id.
- Kinde logout = `redirect("/api/auth/logout")`.

## Work, in this order

### 1. Harden `lib/user.ts`
Replace `'use server'` with `import "server-only"` (after confirming no client
component imports `getUser`). It is a data helper, not a Server Action.

### 2. Rename chat (list page)
- `renameChatAction(chatId, title)` in `chats/actions.ts` (co-located under
  `src/app/users/[userId]/chats/`): authenticate from the Kinde session, trim, reject
  blank, verify ≤ 120 chars, `fetchBackend(PATCH ...)`, then `revalidatePath`.
- UI: `DropdownMenu` (⋯) with **Rename** + **Delete** on each chat `Card`. Rename opens a
  `Dialog` with an `Input` `maxLength={120}`, submit disabled when empty or unchanged; on
  422 show `error.detail` inline in the dialog.

### 3. Delete chat (list page)
- `deleteChatAction(chatId)`: authenticate, `DELETE`, treat 404 as success,
  `revalidatePath`. On **409** return the detail string — do not navigate.
- UI: `AlertDialog` confirm. Copy: "This chat will be removed from your chat list."
  (Chat delete is a **soft** delete — don't claim permanent erasure.) Show the 409
  `detail` as the error message.

### 4. Account deletion (local-only)
- `deleteAccountAction(confirmation)` in a new `src/app/users/[userId]/actions.ts`,
  server-side in this exact order:
  1. Resolve Kinde session → local user via `getUser`. No session / no local user →
     `redirect("/api/auth/logout")`.
  2. Re-verify `confirmation` against the user's email **on the server**.
  3. `fetchBackend("/api/v1/me/data", { method: "DELETE" })`. On **any** failure (401,
     409, 5xx, network) **stop and return the error** — the `users` row must still exist
     so they can retry.
  4. Delete the `users` row via Prisma by `id`. This is Prisma 8 RC — check
     `prisma-8.md`/generated types for the exact delete syntax (the pattern seen is
     `prisma.orm.public.User.where((u) => u.id.eq(localUserId)).delete()`); do not guess,
     do not fall back to raw SQL.
  5. **Skip the Kinde delete (local-only).**
  6. `redirect("/api/auth/logout")`. Never call `getUser`/sync again after step 4
     (`/api/auth/sync` would recreate the user).
- `redirect()` throws — do not wrap it in a try/catch that swallows it. Every step is
  safe to retry.
- UI: client `delete-account-dialog.tsx` next to the page — `AlertDialog`, type-your-email
  `Input`, destructive button **disabled until the email matches**, pending state via
  `useTransition`/`useActionState`, no double-submit, inline error. Copy for
  **local-only**: "This deletes all your data. Signing in again creates a new empty
  account." (Do not say the account can no longer be used.)

### 5. Docs + verification
- Update `AGENTS.md`: chats writes only through the backend; the local-only deletion
  order; the `lib/user.ts` `server-only` change.
- Verify: `pnpm lint`, `pnpm build`; rename (list updates, `updated` timestamp bumps,
  blank/121-char rejected); delete chat (disappears, URL 404s, 409 message while
  streaming); account deletion **on a throwaway Kinde signup** (two chats, one parked at
  the approval gate → delete → logged out; Neon `chats/messages/attachments` + LangGraph
  checkpoint tables empty for that user; sign-in again creates a fresh account); failure
  paths (backend stopped → action errors and user row still exists; delete during an
  active run → 409 message).