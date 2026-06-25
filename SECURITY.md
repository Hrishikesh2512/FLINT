# Security Policy

## Reporting a vulnerability

If you find a security issue, please do not open a public issue. Instead, email
**jha.kharakh@gmail.com** with the details and steps to reproduce. You will get
a response as quickly as possible, and credit if you would like it once the
issue is resolved.

## Scope and good practice

FLINT runs locally and talks to third party APIs (Gemini, optionally Supabase
and OpenRouter). Keep the following in mind:

- API keys and login tokens live only on your machine, in the gitignored files
  listed in the README. Never commit them.
- Only the publishable (anon) Supabase key should ever appear in client side
  files such as the phone remote config.
- Review any automation or tool that can control your machine before running it
  on untrusted input.

## Supported versions

This is an active project. Security fixes target the `master` branch.
