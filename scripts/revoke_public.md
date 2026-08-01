# After the event, revoke everything

Run this list once the demo is over. Nothing here is optional.

1. **Stop the tunnels.** `./scripts/public_demo.sh --stop`. Every public URL dies with them.
2. **Rotate the OpenAI key.** platform.openai.com, API keys, revoke the key named `ClickLiv`.
   It was pasted in plaintext during the build, so it must be replaced rather than reused.
3. **Rotate the Bedrock key.** console.aws.amazon.com, Amazon Bedrock, API keys. Also pasted
   in plaintext. Expires 2026-10-30 on its own, but revoke it sooner.
4. **Reset the ClickHouse Cloud password.**
   `clickhousectl cloud service reset-password b38300d4-e3f6-4a03-83ff-00940cce918d`
5. **Change `MARTS_PASSWORD`** in `.env` and rerun `make marts`. That password reaches the
   Vercel deployment if the public dashboard was deployed, so treat it as exposed.
6. **Delete the Vercel project** if one was created, or at minimum clear its environment
   variables.
7. **Delete the LibreChat demo account.** It has a weak, published password.
   Registration is already disabled, so no new accounts can appear.
8. **Rotate the Langfuse keys** in `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`)
   and the initial user password.
9. **Delete the S3 IAM user** `clickliv-langfuse-s3` and its access key.
10. **Delete the managed Postgres service** if it is no longer wanted:
    `clickhousectl cloud postgres delete 2998952a-d0a2-8ad0-af73-f1a1710b56bd`

The ClickHouse Cloud service itself has idle scaling on with a 15 minute timeout, so it
parks itself. Deleting it is a separate decision.
