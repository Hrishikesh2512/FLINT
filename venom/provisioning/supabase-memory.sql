-- Venom cloud memory backup — one-time Supabase setup.
-- Run this once in your Supabase project: Dashboard → SQL Editor → New query.
--
-- Stores one opaque, ENCRYPTED row per device (keyed by a hash of the memory
-- passphrase). The anon/publishable key is public and extractable from a Pi,
-- so nothing personal is ever stored in the clear here: a row holds only an
-- opaque id + a random salt + ciphertext. Restoring requires the passphrase.

create table if not exists public.venom_memory (
    id         text primary key,           -- sha256(salt + passphrase), opaque
    salt       text        not null,        -- per-row PBKDF2 salt (base64)
    payload    text        not null,        -- Fernet ciphertext of memory.json
    updated_at timestamptz default now()
);

alter table public.venom_memory enable row level security;

-- A device has no login; it authenticates only with the anon key. Because
-- every row is opaque + encrypted and addressed by an unguessable id, we let
-- the anon role read/insert/update rows — but NOT delete (no delete policy),
-- so a leaked anon key can't wipe backups.

drop policy if exists venom_memory_select on public.venom_memory;
create policy venom_memory_select on public.venom_memory
    for select to anon using (true);

drop policy if exists venom_memory_insert on public.venom_memory;
create policy venom_memory_insert on public.venom_memory
    for insert to anon with check (true);

drop policy if exists venom_memory_update on public.venom_memory;
create policy venom_memory_update on public.venom_memory
    for update to anon using (true) with check (true);
