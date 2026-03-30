/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
  extractMessageContent as baileysExtractMessageContent,
} from '@whiskeysockets/baileys';

const GROUP_CACHE_TTL_MS = 5 * 60 * 1000;
const NUMERIC_MENTION_RE = /(^|\s)@([+]?\d[\d\s().-]{3,25}\d)(?=$|\s|[!?,.:;])/g;
const ME_MENTION_RE = /(^|\s)@me(?=$|\s|[!?,.:;])/gi;
const NAME_MENTION_RE = /(^|\s)@([\p{L}][\p{L}\p{N}_.-]{1,31})(?=$|\s|[!?,.:;])/gu;

type GroupMentionContext = {
  participants: string[];
  idToJid: Map<string, string>;
  nameToJid: Map<string, string | null>;
  cachedAt: number;
};

function digitsOnly(value: string): string {
  return value.replace(/\D+/g, '');
}

function localPart(jid: string): string {
  return jid.includes('@') ? jid.split('@')[0] : jid;
}

function normalizeName(value: string): string {
  return value
    .toLowerCase()
    .normalize('NFKC')
    .replace(/[^\p{L}\p{N}]+/gu, '');
}

type MentionReplacement = {
  start: number;
  end: number;
  text: string;
};

function mentionHandleFromJid(jid: string): string {
  const lp = localPart(jid);
  const digits = digitsOnly(lp);
  return digits.length >= 5 ? digits : lp;
}

function applyReplacements(text: string, replacements: MentionReplacement[]): string {
  if (replacements.length === 0) return text;

  const sorted = [...replacements].sort((a, b) => b.start - a.start);
  let out = text;
  for (const r of sorted) {
    out = `${out.slice(0, r.start)}${r.text}${out.slice(r.end)}`;
  }
  return out;
}

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { readFile, writeFile, mkdir } from 'fs/promises';
import { join, basename } from 'path';
import { randomBytes } from 'crypto';

const VERSION = '0.1.0';

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  wasMentioned?: boolean;
  participant?: string;
  media?: string[];
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  private groupContextByJid = new Map<string, GroupMentionContext>();
  private lastGroupSenderByJid = new Map<string, string>();

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  private normalizeJid(jid: string | undefined | null): string {
    const raw = (jid || '').trim();
    if (!raw) return '';

    // Canonical form for mention matching across variants:
    // - 628xxx:12@s.whatsapp.net -> 628xxx
    // - 628xxx@s.whatsapp.net    -> 628xxx
    // - 242xxx:12@lid            -> 242xxx
    // - 242xxx@lid               -> 242xxx
    let v = raw;
    if (v.includes('@')) v = v.split('@')[0];
    if (v.includes(':')) v = v.split(':')[0];
    return v;
  }

  private wasMentioned(msg: any): boolean {
    if (!msg?.key?.remoteJid?.endsWith('@g.us')) return false;

    const candidates = [
      msg?.message?.extendedTextMessage?.contextInfo?.mentionedJid,
      msg?.message?.imageMessage?.contextInfo?.mentionedJid,
      msg?.message?.videoMessage?.contextInfo?.mentionedJid,
      msg?.message?.documentMessage?.contextInfo?.mentionedJid,
      msg?.message?.audioMessage?.contextInfo?.mentionedJid,
    ];
    const mentioned = candidates.flatMap((items) => (Array.isArray(items) ? items : []));
    if (mentioned.length === 0) return false;

    const selfIds = new Set(
      [this.sock?.user?.id, this.sock?.user?.lid, this.sock?.user?.jid]
        .map((jid) => this.normalizeJid(jid))
        .filter(Boolean),
    );
    return mentioned.some((jid: string) => selfIds.has(this.normalizeJid(jid)));
  }

  async connect(): Promise<void> {
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nanobot', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        if (msg.key.fromMe) continue;
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const unwrapped = baileysExtractMessageContent(msg.message);
        if (!unwrapped) continue;

        const content = this.getTextContent(unwrapped);
        let fallbackContent: string | null = null;
        const mediaPaths: string[] = [];

        if (unwrapped.imageMessage) {
          fallbackContent = '[Image]';
          const path = await this.downloadMedia(msg, unwrapped.imageMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.documentMessage) {
          fallbackContent = '[Document]';
          const path = await this.downloadMedia(msg, unwrapped.documentMessage.mimetype ?? undefined,
            unwrapped.documentMessage.fileName ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.videoMessage) {
          fallbackContent = '[Video]';
          const path = await this.downloadMedia(msg, unwrapped.videoMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        }

        const finalContent = content || (mediaPaths.length === 0 ? fallbackContent : '') || '';
        if (!finalContent && mediaPaths.length === 0) continue;

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;
        const wasMentioned = this.wasMentioned(msg);
        const participantJid = isGroup && typeof msg.key.participant === 'string' ? msg.key.participant : '';
        if (isGroup && msg.key.remoteJid) {
          const pushName = typeof msg.pushName === 'string' ? msg.pushName : undefined;
          if (participantJid) {
            this.learnGroupParticipant(msg.key.remoteJid, participantJid, pushName);
          }
        }

        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: finalContent,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          ...(isGroup ? { wasMentioned, participant: participantJid } : {}),
          ...(mediaPaths.length > 0 ? { media: mediaPaths } : {}),
        });
      }
    });
  }

  private learnGroupParticipant(groupJid: string, participantJid: string, pushName?: string): void {
    if (!groupJid.endsWith('@g.us')) return;
    if (!participantJid || !participantJid.includes('@')) return;

    this.lastGroupSenderByJid.set(groupJid, participantJid);

    const cached = this.groupContextByJid.get(groupJid);
    if (!cached) return;

    const nextParticipants = cached.participants.includes(participantJid)
      ? cached.participants
      : [...cached.participants, participantJid];

    const nextIdToJid = new Map(cached.idToJid);
    const lp = localPart(participantJid);
    const digits = digitsOnly(lp);
    nextIdToJid.set(lp, participantJid);
    if (digits.length >= 5) nextIdToJid.set(digits, participantJid);

    const nextNameToJid = new Map(cached.nameToJid);
    if (pushName) {
      const key = normalizeName(pushName);
      if (key) {
        const prev = nextNameToJid.get(key);
        if (prev === undefined) {
          nextNameToJid.set(key, participantJid);
        } else if (prev !== participantJid) {
          nextNameToJid.set(key, null);
        }
      }
    }

    this.groupContextByJid.set(groupJid, {
      participants: nextParticipants,
      idToJid: nextIdToJid,
      nameToJid: nextNameToJid,
      cachedAt: Date.now(),
    });
  }

  private async getGroupMentionContext(groupJid: string): Promise<GroupMentionContext | null> {
    const cached = this.groupContextByJid.get(groupJid);
    if (cached && Date.now() - cached.cachedAt < GROUP_CACHE_TTL_MS) {
      return cached;
    }

    if (!this.sock || typeof this.sock.groupMetadata !== 'function') {
      return cached || null;
    }

    try {
      const md = await this.sock.groupMetadata(groupJid);
      const participantsRaw = (md?.participants || []) as any[];
      const participants = participantsRaw
        .map((p) => p?.id || p?.jid)
        .filter((v): v is string => typeof v === 'string' && v.length > 0);
      if (cached?.participants?.length) {
        for (const existing of cached.participants) {
          if (!participants.includes(existing)) participants.push(existing);
        }
      }

      const idToJid = new Map<string, string>();
      const nameCandidates = new Map<string, Set<string>>();

      const addNameCandidate = (name: string | undefined, jid: string) => {
        if (!name) return;
        const key = normalizeName(name);
        if (!key) return;
        if (!nameCandidates.has(key)) nameCandidates.set(key, new Set());
        nameCandidates.get(key)!.add(jid);
      };

      for (const p of participantsRaw) {
        const jid = p?.id || p?.jid;
        if (!jid || typeof jid !== 'string') continue;

        const lp = localPart(jid);
        const digits = digitsOnly(lp);

        idToJid.set(lp, jid);
        if (digits.length >= 5) idToJid.set(digits, jid);

        addNameCandidate(p?.name, jid);
        addNameCandidate(p?.notify, jid);
        addNameCandidate(p?.pushName, jid);
        addNameCandidate(p?.verifiedName, jid);

        const contact = this.sock?.contacts?.[jid];
        if (contact) {
          addNameCandidate(contact?.name, jid);
          addNameCandidate(contact?.notify, jid);
          addNameCandidate(contact?.verifiedName, jid);
          addNameCandidate(contact?.short, jid);
        }
      }

      if (cached?.nameToJid?.size) {
        for (const [k, v] of cached.nameToJid.entries()) {
          if (!v) continue;
          if (!nameCandidates.has(k)) nameCandidates.set(k, new Set());
          nameCandidates.get(k)!.add(v);
        }
      }

      const nameToJid = new Map<string, string | null>();
      for (const [k, v] of nameCandidates.entries()) {
        if (v.size === 1) {
          nameToJid.set(k, [...v][0]);
        } else {
          nameToJid.set(k, null); // ambiguous
        }
      }

      const ctx: GroupMentionContext = {
        participants,
        idToJid,
        nameToJid,
        cachedAt: Date.now(),
      };

      this.groupContextByJid.set(groupJid, ctx);
      return ctx;
    } catch (err) {
      console.warn('Failed to fetch group metadata for mention resolution:', err);
      return cached || null;
    }
  }

  private resolveNameMentionJid(handle: string, ctx: GroupMentionContext): string | null {
    const key = normalizeName(handle);
    if (!key) return null;

    const exact = ctx.nameToJid.get(key);
    if (exact !== undefined) return exact;

    // Fallback: unique prefix match
    let match: string | null = null;
    for (const [k, v] of ctx.nameToJid.entries()) {
      if (!v) continue;
      if (!k.startsWith(key)) continue;
      if (!match) {
        match = v;
      } else if (match !== v) {
        return null; // ambiguous
      }
    }

    return match;
  }

  private async buildOutboundTextPayload(
    to: string,
    text: string,
  ): Promise<{ text: string; mentions?: string[] }> {
    const mentions = new Set<string>();
    const replacements: MentionReplacement[] = [];
    const isGroup = to.endsWith('@g.us');
    const groupCtx = isGroup ? await this.getGroupMentionContext(to) : null;

    NUMERIC_MENTION_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = NUMERIC_MENTION_RE.exec(text)) !== null) {
      const prefix = m[1] || '';
      const raw = m[2];
      const digits = digitsOnly(raw);
      if (digits.length < 5) continue;

      let jid: string | undefined;
      if (groupCtx) {
        jid = groupCtx.idToJid.get(digits);
      }
      if (!jid) {
        jid = `${digits}@s.whatsapp.net`;
      }

      mentions.add(jid);

      const atStart = m.index + prefix.length;
      const atEnd = atStart + 1 + raw.length;
      replacements.push({
        start: atStart,
        end: atEnd,
        text: `@${mentionHandleFromJid(jid)}`,
      });
    }

    if (groupCtx) {
      ME_MENTION_RE.lastIndex = 0;
      while ((m = ME_MENTION_RE.exec(text)) !== null) {
        const prefix = m[1] || '';
        const jid = this.lastGroupSenderByJid.get(to);
        if (!jid) continue;

        mentions.add(jid);

        const atStart = m.index + prefix.length;
        const atEnd = atStart + 3;
        replacements.push({
          start: atStart,
          end: atEnd,
          text: `@${mentionHandleFromJid(jid)}`,
        });
      }

      NAME_MENTION_RE.lastIndex = 0;
      while ((m = NAME_MENTION_RE.exec(text)) !== null) {
        const prefix = m[1] || '';
        const handle = m[2];

        if (handle.toLowerCase() === 'me') continue;
        // Ignore numeric-like tokens handled by numeric parser.
        if (!Number.isNaN(Number(handle))) continue;

        const jid = this.resolveNameMentionJid(handle, groupCtx);
        if (!jid) continue;

        mentions.add(jid);

        const atStart = m.index + prefix.length;
        const atEnd = atStart + 1 + handle.length;
        replacements.push({
          start: atStart,
          end: atEnd,
          text: `@${mentionHandleFromJid(jid)}`,
        });
      }
    }

    const rewrittenText = applyReplacements(text, replacements);
    if (mentions.size === 0) return { text: rewrittenText };

    return { text: rewrittenText, mentions: [...mentions] };
  }

  private async downloadMedia(msg: any, mimetype?: string, fileName?: string): Promise<string | null> {
    try {
      const mediaDir = join(this.options.authDir, '..', 'media');
      await mkdir(mediaDir, { recursive: true });

      const buffer = await downloadMediaMessage(msg, 'buffer', {}) as Buffer;

      let outFilename: string;
      if (fileName) {
        // Documents have a filename — use it with a unique prefix to avoid collisions
        const prefix = `wa_${Date.now()}_${randomBytes(4).toString('hex')}_`;
        outFilename = prefix + fileName;
      } else {
        const mime = mimetype || 'application/octet-stream';
        // Derive extension from mimetype subtype (e.g. "image/png" → ".png", "application/pdf" → ".pdf")
        const ext = '.' + (mime.split('/').pop()?.split(';')[0] || 'bin');
        outFilename = `wa_${Date.now()}_${randomBytes(4).toString('hex')}${ext}`;
      }

      const filepath = join(mediaDir, outFilename);
      await writeFile(filepath, buffer);

      return filepath;
    } catch (err) {
      console.error('Failed to download media:', err);
      return null;
    }
  }

  private getTextContent(message: any): string | null {
    // Text message
    if (message.conversation) {
      return message.conversation;
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return message.extendedTextMessage.text;
    }

    // Image with optional caption
    if (message.imageMessage) {
      return message.imageMessage.caption || '';
    }

    // Video with optional caption
    if (message.videoMessage) {
      return message.videoMessage.caption || '';
    }

    // Document with optional caption
    if (message.documentMessage) {
      return message.documentMessage.caption || '';
    }

    // Voice/Audio message
    if (message.audioMessage) {
      return `[Voice Message]`;
    }

    return null;
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    const payload = await this.buildOutboundTextPayload(to, text);
    await this.sock.sendMessage(to, payload);
  }

  async markRead(chatId: string, messageId: string, participant?: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    if (!chatId || !messageId) return;

    const key: Record<string, unknown> = {
      remoteJid: chatId,
      id: messageId,
      fromMe: false,
    };

    if (chatId.endsWith('@g.us') && participant) {
      key.participant = participant;
    }

    await this.sock.readMessages([key]);
  }

  async sendMedia(
    to: string,
    filePath: string,
    mimetype: string,
    caption?: string,
    fileName?: string,
  ): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    const buffer = await readFile(filePath);
    const category = mimetype.split('/')[0];

    if (category === 'image') {
      await this.sock.sendMessage(to, { image: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'video') {
      await this.sock.sendMessage(to, { video: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'audio') {
      await this.sock.sendMessage(to, { audio: buffer, mimetype });
    } else {
      const name = fileName || basename(filePath);
      await this.sock.sendMessage(to, { document: buffer, mimetype, fileName: name });
    }
  }

  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
