# Release to Homebrew

Release fwts to PyPI and update the Homebrew formula.

## Steps

1. **Build the package**
   ```bash
   cd /Users/laudiacay/code/featurebox
   rm -rf dist/
   uv build
   ```

2. **Upload to PyPI**
   ```bash
   uv publish
   ```
   This will prompt for PyPI credentials if not cached.

3. **Get the new package SHA256**
   Wait a few seconds for PyPI to process, then:
   ```bash
   VERSION=$(grep '^version' pyproject.toml | cut -d'"' -f2)
   curl -sL "https://files.pythonhosted.org/packages/source/f/fwts/fwts-${VERSION}.tar.gz" | shasum -a 256
   ```

   If that fails (package not yet available), try fetching from PyPI API:
   ```bash
   curl -s "https://pypi.org/pypi/fwts/${VERSION}/json" | jq -r '.urls[] | select(.packagetype == "sdist") | .digests.sha256'
   ```

4. **Update the Homebrew formula**
   Edit `/Users/laudiacay/code/homebrew-tap/Formula/fwts.rb`:
   - Update the `url` line with new version
   - Update the `sha256` line with the new hash

   The URL format is:
   ```
   https://files.pythonhosted.org/packages/source/f/fwts/fwts-VERSION.tar.gz
   ```

5. **Commit and push the formula**
   ```bash
   cd /Users/laudiacay/code/homebrew-tap
   git add Formula/fwts.rb
   git commit -m "fwts VERSION"
   git push
   ```

6. **Reinstall locally**
   ```bash
   brew update
   brew upgrade fwts
   ```

   Or if that doesn't pick up the change:
   ```bash
   brew reinstall fwts
   ```

## Quick Version (after manual steps above)

Once you have the sha256, you can run this to complete the release:
```bash
VERSION="0.1.XX"  # Set this
SHA256="abc123..."  # Set this

# Update formula
cd /Users/laudiacay/code/homebrew-tap
sed -i '' "s|url \".*\"|url \"https://files.pythonhosted.org/packages/source/f/fwts/fwts-${VERSION}.tar.gz\"|" Formula/fwts.rb
sed -i '' "s|sha256 \".*\"|sha256 \"${SHA256}\"|" Formula/fwts.rb

# Commit and push
git add Formula/fwts.rb && git commit -m "fwts ${VERSION}" && git push

# Reinstall
brew update && brew upgrade fwts
```
