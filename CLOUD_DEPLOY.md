# Flight Brief Cloud Builder

This deploys the same brief builder as a cloud web app so iPhone/iPad can upload PDFs and generate without the Mac.

## Render setup

1. Push this folder to a GitHub/GitLab/Bitbucket repository.
2. Open Render Blueprint deploy:
   `https://dashboard.render.com/blueprint/new`
3. Select the repository.
4. Render will read `render.yaml` and create `flight-brief-cloud`.
5. After deploy, open the Render service URL on iPhone/iPad.

## Cloud behavior

- Upload Flight Plan, Trip Kit, and Pairing on the cloud page.
- Tap `Generate brief`.
- Generated outputs are available from the same cloud page.
- The Mac-only `Publish to iPhone` flow is hidden in cloud mode.

## Notes

- Render free services can sleep after inactivity, so the first load may take a little longer.
- Files are stored in `/tmp/flight-brief-output`, so they may reset on redeploy/restart. Generate the brief again if needed.
- The Mac app still works locally and can still publish to the Cloudflare Pages viewer.
