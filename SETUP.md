# Setup — Three-Click Version

I can't create your GitHub repo for you (needs your login). But I made everything else as close to one-click as possible.

## Three things you do, in order

### 1. Create the empty repo on GitHub (30 seconds)

1. Go to **https://github.com/new**
2. Repo name: `nhl-props-model`
3. **Public** (Pages is free for public repos; private requires Pro)
4. **DO NOT** check "Add a README", "Add .gitignore", or "Add license" — we have those already
5. Click "Create repository"

### 2. Run the helper script (1 minute)

In a Windows terminal:
```
cd C:\Users\qrob1\Documents\Claude\Projects\nhl
setup_github.bat
```

It'll:
- Ask your GitHub username
- Build the dashboard locally first (so Pages works immediately)
- Initialize git, commit, push

If git asks for password, paste a Personal Access Token (not your GitHub password — GitHub disabled that years ago):
- Get one at: https://github.com/settings/tokens
- Click "Generate new token (classic)"
- Scopes: tick `repo` and `workflow`
- Copy the token, paste when prompted

### 3. Three clicks in GitHub (2 minutes)

After the push succeeds, the script prints three URLs. Click each:

**A. Add the API key secret** (so the workflow can call DK)

URL: `github.com/YOUR_USERNAME/nhl-props-model/settings/secrets/actions/new`

- Name: `ODDS_API_KEY`
- Value: `5cce14f3242989037557db8157e2db7f`
- Click "Add secret"

**B. Enable GitHub Pages**

URL: `github.com/YOUR_USERNAME/nhl-props-model/settings/pages`

- Source: "Deploy from a branch"
- Branch: `main`
- Folder: `/docs`
- Click "Save"

**C. Run the first build**

URL: `github.com/YOUR_USERNAME/nhl-props-model/actions`

- Click "Daily NHL Props Build" in left sidebar
- Click "Run workflow" (gray button, top right)
- Click green "Run workflow" inside the dropdown

Wait ~2 minutes. Refresh the Actions page. You'll see a green check when it's done.

## Your dashboard URL

After the first successful build:
```
https://YOUR_USERNAME.github.io/nhl-props-model/
```

Bookmark it on your phone. From now on it auto-rebuilds at 12pm/4pm/7pm ET daily — you don't have to do anything.

## Troubleshooting

**Git push fails with "Authentication failed"**
- You need a Personal Access Token, not a password. See step 2 above.

**Workflow fails on first run with "ODDS_API_KEY not set"**
- Add the secret per step 3A.

**Pages shows 404**
- Settings → Pages → check Source is `main` + `/docs`
- After enabling, takes ~1 min for first deploy. Refresh.

**Workflow fails with "Permission denied"**
- Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save

**Need to refresh data NOW (not waiting for the next scheduled run)**
- Actions tab → Daily NHL Props Build → Run workflow → green button
- Takes ~90 seconds
