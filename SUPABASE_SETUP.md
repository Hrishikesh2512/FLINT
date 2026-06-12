# Supabase setup

FLINT uses Supabase for **accounts** (email/password sign-in) and, optionally,
the **phone remote** command queue. This takes about 5 minutes.

## 1. Create a project

1. Sign in at https://supabase.com/dashboard and click **New project**.
2. Pick a name, a strong database password, and a region near you.
3. Wait for it to finish provisioning.

## 2. Get your URL + publishable key

1. **Project Settings** (gear icon) → **API**.
2. Copy the **Project URL** and the **anon / public** key (newer projects label
   it **publishable**, e.g. `sb_publishable_…` or a long `eyJ…`).
3. Copy the template and paste them in:
   ```bash
   cp config/app_config.example.json config/app_config.json
   ```
   ```json
   {
     "supabase_url": "https://YOUR-PROJECT-ref.supabase.co",
     "supabase_anon_key": "YOUR-PUBLISHABLE-ANON-KEY"
   }
   ```

> ⚠️ Only ever use the **publishable / anon** key here. Never the
> **service_role / secret** key — it bypasses all security and would be
> extractable from the shared build.

## 3. Turn off email confirmation (required)

Without this, new users can't sign in on a second machine because the free
built-in mailer won't reliably deliver confirmation emails.

1. **Authentication** → **Sign In / Providers** (older UI: **Providers**) → **Email**.
2. Turn **off** **"Confirm email"** → **Save**.
3. Make sure **Email** is enabled and new sign-ups are allowed.

Now sign-up returns a usable session immediately — no email step.

## 4. See your users

**Authentication → Users** lists every account: email, created date, last
sign-in. That's your "who's using FLINT" view. You never see passwords (Supabase
stores only salted hashes).

## 5. Phone remote table (optional)

Only needed for the `mobile_ui/` phone console. **Database → SQL Editor → New
query**, paste, **Run**:

```sql
create table if not exists public.command_queue (
  id          bigint generated always as identity primary key,
  action_name text not null,
  status      text not null default 'pending',
  payload     jsonb,
  result      text,
  error       text,
  created_at  timestamptz not null default now(),
  executed_at timestamptz
);

alter table public.command_queue enable row level security;

-- The phone (anon) may ONLY insert rows that start as 'pending'.
create policy "anon can enqueue pending commands"
  on public.command_queue
  for insert to anon
  with check (status = 'pending');
```

See [mobile_ui/README.md](mobile_ui/README.md) for the full phone setup and RLS notes.

> Heads-up: as written the queue is shared across all users. For your own use
> that's fine; for many users you'd add a `user_id` column and per-user policies.
