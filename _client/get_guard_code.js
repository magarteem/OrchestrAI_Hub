/**
 * Generates Steam Guard code from shared_secret (maFile).
 * Usage: node get_guard_code.js <shared_secret_base64>
 * Output: 5-character code to stdout
 */
const SteamTotp = require('steam-totp');

const args = process.argv.slice(2);
if (args.length < 1) {
    process.stderr.write('Usage: node get_guard_code.js <shared_secret>\n');
    process.exit(1);
}

try {
    const code = SteamTotp.getAuthCode(args[0]);
    process.stdout.write(code + '\n');
} catch (err) {
    process.stderr.write(err.message + '\n');
    process.exit(1);
}
