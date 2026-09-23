// Smoke-test edge function. Reachable at:
//   GET http://<box>:${PORT_SUPABASE_API}/functions/v1/hello
//
// Upstream's sample `hello` function imports `@supabase/server` and
// authenticates with the new publishable / secret API keys. This deployment
// runs in LEGACY HS256 API-key mode (SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY,
// with SUPABASE_PUBLISHABLE_KEY / SUPABASE_SECRET_KEY deliberately empty), so
// that sample 401s here. A plain Deno.serve handler proves the edge runtime,
// the Envoy route and the JWT policy without depending on either key style.
//
// SUPABASE_FUNCTIONS_VERIFY_JWT=false in .env means this answers unauthenticated.
// Flip it to true and the gateway will require an `Authorization: Bearer <anon key>`.

Deno.serve((req: Request) => {
  return Response.json({
    message: "hello from supabase edge functions",
    method: req.method,
    timestamp: new Date().toISOString(),
  });
});
