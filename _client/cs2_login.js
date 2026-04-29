// cs2_login.js v1.0
// Steam login with TOTP 2FA. Reports status via stdout JSON lines.
// Steam login + TOTP (раньше — вариант из FSM activity_booster)
//
// Args: login password shared_secret machine_name [totp_override]
// stdout protocol:
//   {"event":"logged_in","steamid":"..."}
//   {"event":"error","code":N,"message":"..."}
//   {"event":"disconnected"}

const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');

const [,, login, password, shared_secret, machine_name, totp_override] = process.argv;

if (!login || !password || !shared_secret) {
    emit({ event: 'error', code: -1, message: 'Missing required args: login password shared_secret' });
    process.exit(1);
}

const client = new SteamUser();

function emit(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}

const twoFactorCode = totp_override || SteamTotp.getAuthCode(shared_secret);

const logOnOptions = {
    accountName: login,
    password: password,
    twoFactorCode: twoFactorCode,
    machineName: machine_name || 'CS2Farm'
};

client.logOn(logOnOptions);

client.on('loggedOn', () => {
    client.setPersona(SteamUser.EPersonaState.Online);
    emit({ event: 'logged_in', steamid: client.steamID ? client.steamID.toString() : null });
    // Keep process alive — vm_agent will kill it when needed
});

client.on('steamGuard', (domain, callback, lastCodeWrong) => {
    if (lastCodeWrong) {
        emit({ event: 'error', code: -2, message: 'Steam Guard code was wrong' });
        process.exit(4);
    }
    // Regenerate TOTP and retry
    const code = SteamTotp.getAuthCode(shared_secret);
    emit({ event: 'steam_guard', domain: domain, code: code });
    callback(code);
});

client.on('error', (err) => {
    emit({ event: 'error', code: err.eresult || -1, message: err.message || String(err) });
    process.exit(4);
});

client.on('disconnected', (eresult, msg) => {
    emit({ event: 'disconnected', code: eresult, message: msg });
    process.exit(1);
});

// Graceful shutdown on SIGTERM from vm_agent
process.on('SIGTERM', () => {
    client.logOff();
    process.exit(0);
});
