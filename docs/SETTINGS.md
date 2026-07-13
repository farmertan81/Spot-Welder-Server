# Settings Configuration

## How It Works

This project uses a **template system** for settings files:

### Template File (in Git)
- `settings.default.json` - Default weld/server settings template (repo root)

This file is **committed to Git** and provides sensible defaults for new users.

### Runtime File (NOT in Git)
- `settings.json` - Your personal runtime settings (auto-created from the template)

This file is **.gitignored** so your personal settings never get committed!

## First Run Setup

On first run, the Flask server automatically copies:
```
settings.default.json → settings.json
```

You can then modify `settings.json` with your personal values:
- Lead resistance
- Weld power/duration
- Joule mode targets
- etc.

**Your changes stay local and won't be pushed to GitHub!** ✅

## Updating Defaults

If you want to change the default template (for all users):
1. Edit `settings.default.json` (the template)
2. Commit and push to GitHub
3. New clones will get your updated defaults

## Resetting to Defaults

To reset your settings:
```bash
cp settings.default.json settings.json
```

Or just delete `settings.json` and restart the server (auto-recreates from template).

## Why This Approach?

✅ Default settings in repo (easy for new users)  
✅ Personal settings stay local (no commit spam)  
✅ Easy to reset if you mess up  
✅ No merge conflicts on settings  
