const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";

const IA_SCHEMA = `{
  "titulo": "Relatório estratégico de engajamento",
  "resumo_executivo": "2-4 parágrafos em linguagem clara para a cliente",
  "paginas_destaque": [
    {"pagina": "nome", "handle": "@...", "status": "em alta|estável|precisa atenção", "porque_funciona": "...", "vertente_conteudo": "..."}
  ],
  "padroes_vencedores": "O que os posts de maior engajamento têm em comum",
  "paginas_melhorar": [
    {"pagina": "nome", "diagnostico": "...", "acoes": ["ação 1", "ação 2"]}
  ],
  "plano_conteudo": {
    "vertente_principal": "linha editorial que está performando",
    "replicar": ["recomendação 1", "recomendação 2"],
    "evitar": ["o que não está funcionando"],
    "frequencia_sugerida": "sugestão prática"
  },
  "proximos_passos": ["passo 1", "passo 2", "passo 3"]
}`;

function buildPrompt(context) {
  return (
    "Você é analista de conteúdo Instagram para uma rede de páginas de notícias regionais do Rio de Janeiro.\n" +
    "Analise os dados de engajamento (últimas publicações por perfil) e produza um relatório estratégico para a cliente.\n" +
    "Explique: quais páginas dão certo, POR QUE dão certo, como melhorar as que estão fracas, " +
    "e como trilhar novos posts na mesma vertente do que performa.\n" +
    "Cite páginas, handles e números reais dos dados. Tom profissional e acessível. Português do Brasil.\n\n" +
    `DADOS:\n${JSON.stringify(context, null, 2)}\n\n` +
    `Retorne APENAS JSON válido com esta estrutura:\n${IA_SCHEMA}`
  );
}

function parseJsonContent(content) {
  let text = (content || "").trim();
  if (text.startsWith("```")) {
    text = text.replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "");
  }
  return JSON.parse(text);
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Método não permitido" });
  }

  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey || !apiKey.trim()) {
    return res.status(503).json({ error: "OPENROUTER_API_KEY não configurada na Vercel" });
  }

  const context = req.body?.context;
  if (!context || !context.paginas) {
    return res.status(400).json({ error: "Dados de engajamento inválidos" });
  }

  const model = process.env.OPENROUTER_MODEL || "google/gemini-2.5-flash";

  try {
    const response = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey.trim()}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://clauthub.digital",
        "X-Title": "Clauth Hub Engajamento",
      },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: buildPrompt(context) }],
        response_format: { type: "json_object" },
      }),
    });

    if (!response.ok) {
      const errText = await response.text();
      return res.status(502).json({ error: `OpenRouter: ${response.status}`, detail: errText.slice(0, 200) });
    }

    const payload = await response.json();
    const content = payload?.choices?.[0]?.message?.content;
    const analise = parseJsonContent(content);
    analise.gerado_em = new Date().toISOString();
    analise.modelo = model;

    return res.status(200).json({ analise });
  } catch (err) {
    return res.status(500).json({ error: err.message || "Erro ao gerar análise" });
  }
}
