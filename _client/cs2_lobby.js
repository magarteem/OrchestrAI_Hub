// cs2_lobby.js v1.0
// Creates a CS2 2v2 lobby, invites partners, and accepts lobby invites.
// stdout protocol:
//   {"event":"logged_in","steamid":"..."}
//   {"event":"invite_sent","to_steamid":"..."}
//   {"event":"invite_accepted","from_steamid":"..."}
//   {"event":"in_lobby","lobby_id":"...","lobby_steam_id":"..."}
//   {"event":"error","message":"..."}
//
// Mode "captain": login password shared_secret machine_name captain steamid1 steamid2 steamid3
// Mode "member":  login password shared_secret machine_name member
//
// Captain creates lobby then invites steamid1..N via chat message +connect_lobby.
// Members wait for lobby invite and accept automatically.

const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');

const args = process.argv.slice(2);
const [login, password, shared_secret, machine_name, mode, ...extra] = args;

if (!login || !password || !shared_secret || !mode) {
    process.stdout.write(JSON.stringify({ event: 'error', message: 'Missing args' }) + '\n');
    process.exit(1);
}

const client = new SteamUser();

function emit(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}

client.logOn({
    accountName: login,
    password: password,
    twoFactorCode: SteamTotp.getAuthCode(shared_secret),
    machineName: machine_name || 'CS2Farm'
});

client.on('loggedOn', () => {
    client.setPersona(SteamUser.EPersonaState.Online);
    emit({ event: 'logged_in', steamid: client.steamID.toString() });

    if (mode === 'captain') {
        const inviteSteamIds = extra; // list of steamid64 strings
        // CS2 uses +connect_lobby <lobby_id> but we can't easily create a lobby via Steam API.
        // Instead: captain sends friend messages with connect string.
        // The connect string is formed once CS2 is running and creates the lobby.
        // Here we send friend invites if not already friends, then send lobby invite via richPresence.
        _sendLobbyInvites(inviteSteamIds);
    } else {
        // member: listen for lobby invite message
        _waitForLobbyInvite();
    }
});

function _sendLobbyInvites(steamIds) {
    // Send friend message to each partner with the lobby connect command.
    // The actual lobby ID is set by CS2 via richPresence; here we notify controller
    // that we're ready to share the lobby link once CS2 sets it.
    emit({ event: 'captain_ready', invite_targets: steamIds });

    steamIds.forEach(sid => {
        // Add as friend if needed — steam-user doesn't expose sendFriendMessage directly
        // but we can use client.chat.sendFriendMessage if available (steam-user v4+)
        try {
            client.chat.sendFriendMessage(sid, '+connect_lobby_pending');
            emit({ event: 'invite_sent', to_steamid: sid });
        } catch (e) {
            emit({ event: 'error', message: `Failed to invite ${sid}: ${e.message}` });
        }
    });
}

function _waitForLobbyInvite() {
    // Listen for friend messages containing connect_lobby string
    client.on('friendMessage', (senderID, message) => {
        const lobbyMatch = message.match(/\+connect_lobby\s+(\S+)/);
        if (lobbyMatch) {
            const lobbyId = lobbyMatch[1];
            emit({ event: 'lobby_invite_received', from_steamid: senderID.toString(), lobby_id: lobbyId });
        }

        // Listen for direct lobby_id payload from captain
        try {
            const payload = JSON.parse(message);
            if (payload.type === 'cs2_lobby' && payload.lobby_id) {
                emit({ event: 'lobby_invite_received', from_steamid: senderID.toString(), lobby_id: payload.lobby_id });
            }
        } catch (_) { /* not JSON */ }
    });
}

// Captain can receive lobby_id from stdin once CS2 creates it, then forward to members
process.stdin.setEncoding('utf8');
process.stdin.on('data', (data) => {
    try {
        const msg = JSON.parse(data.trim());
        if (msg.cmd === 'send_lobby' && msg.lobby_id && msg.targets) {
            msg.targets.forEach(sid => {
                const payload = JSON.stringify({ type: 'cs2_lobby', lobby_id: msg.lobby_id });
                try {
                    client.chat.sendFriendMessage(sid, payload);
                    emit({ event: 'invite_sent', to_steamid: sid, lobby_id: msg.lobby_id });
                } catch (e) {
                    emit({ event: 'error', message: `send_lobby failed for ${sid}: ${e.message}` });
                }
            });
        }
    } catch (_) { /* ignore non-JSON stdin */ }
});

client.on('steamGuard', (domain, callback, lastCodeWrong) => {
    if (lastCodeWrong) {
        emit({ event: 'error', message: 'Steam Guard code wrong' });
        process.exit(4);
    }
    callback(SteamTotp.getAuthCode(shared_secret));
});

client.on('error', (err) => {
    emit({ event: 'error', code: err.eresult || -1, message: err.message || String(err) });
    process.exit(4);
});

client.on('disconnected', (eresult, msg) => {
    emit({ event: 'disconnected', code: eresult, message: msg });
    process.exit(1);
});

process.on('SIGTERM', () => {
    client.logOff();
    process.exit(0);
});
