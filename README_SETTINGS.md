# Settings Configuration

## How It Works

This project uses a **template system** for settings files:

### Template Files (in Git)
- `settings.default.json` - Default weld settings template
- `rpi/server/settings.default.json` - Server-specific settings template

These files are **committed to Git** and provide sensible defaults for new users.

### Runtime Files (NOT in Git)
- `settings.json` - Your personal weld settings (auto-created from template)
- `rpi/server/settings.json` - Your server runtime settings (auto-created)

These files are **.gitignored** so your personal settings never get committed!

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
cp rpi/server/settings.default.json rpi/server/settings.json
```

Or just delete `settings.json` and restart the server (auto-recreates from template).

## Why This Approach?

✅ Default settings in repo (easy for new users)  
✅ Personal settings stay local (no commit spam)  
✅ Easy to reset if you mess up  
✅ No merge conflicts on settings  
