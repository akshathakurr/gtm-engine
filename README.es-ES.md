

# GTM Engine

<p align="center">
  <img src="gtm-engine-hero.png" alt="GTM Engine ejecutándose en Claude Code" width="100%">
</p>

tu equipo de GTM es una carpeta con scripts de Python y un archivo Markdown que describe tu negocio. ábrelo en Claude Code, responde unas preguntas y lanza campañas.

**esta es la pila de ventas / marketing / contenido que realmente posees.**

sin suscripciones SaaS, sin bloqueo de datos, sin precios por asiento. tú aportas una clave de Anthropic y un token de Apify. Claude Code controla todo.

## qué hace

6 flujos de trabajo que cubren la mayor parte del GTM:

| flujo de trabajo | qué hace | salida |
|---|---|---|
| linkedin outreach | buscar prospectos, extraer sus publicaciones y redactar mensajes directos personalizados | hoja de cálculo de Google con prospectos + mensajes borrador |
| email outreach | enriquecer una lista de empresas por señales de compra y redactar correos en frío personalizados | CSV listo para Instantly / Smartlead |
| competitor analysis | investigar 12 dimensiones de cada competidor | hoja de cálculo de Google completada (firmografía, fundadores, GTM, puntuación) |
| content idea finder | escanear Twitter + HN diariamente y agrupar en ideas de publicaciones | 5 ideas diarias, clasificadas por género + plataforma |
| linkedin comment helper | mostrar publicaciones de LinkedIn que valga la pena comentar | lista clasificada con perspectivas sugeridas |
| blog builder | investigar un tema en profundidad y redactar un artículo de blog | artículo completo + fuentes |

## cómo usarlo

```bash
git clone https://github.com/akshathakurr/gtm-engine
cd gtm-engine
claude "help me get started"
```

esa última línea abre Claude Code e inicia todo automáticamente: recibirás un saludo y unas preguntas, sin tener que adivinar qué escribir.

Claude Code se encarga del resto: configura tus archivos, te guía para ingresar tus claves, te entrevista sobre tu negocio, completa tu contexto y te muestra los flujos de trabajo que puedes ejecutar. esa es toda la UX. no se requiere Python.

¿aún no tienes Claude Code? instálalo primero — [claude.com/claude-code](https://claude.com/claude-code). ¿ya estás dentro de Claude Code o quieres iniciarlo tú mismo? simplemente di `help me get started`.

si prefieres ejecutar las cosas directamente, cada flujo de trabajo tiene su propio README con los comandos exactos de `python -m ...`.

## qué necesitarás

- **clave de API de Anthropic** — Claude hace el trabajo cognitivo. ~$1–5 por ejecución de flujo de trabajo, dependiendo de cuál sea. [console.anthropic.com](https://console.anthropic.com)
- **token de Apify** — para extracción de datos en LinkedIn / Twitter / reseñas. pago por ejecución, generalmente $0.50–$3 por flujo de trabajo. [apify.com](https://apify.com)
- **clave de búsqueda web — exa *o* parallel** *(opcional)* — para investigación en la web. necesario para análisis de competidores, generador de blogs y ambos flujos de prospección. cualquiera de los dos funciona; configura ambos y Exa será el principal con Parallel como respaldo automático. [exa.ai](https://exa.ai) / [platform.parallel.ai](https://platform.parallel.ai)
- **clave de Firecrawl** *(opcional)* — solo para análisis de competidores. lee páginas con mucho JavaScript (precios, estudios de caso) que el extractor básico no puede. la capa gratuita (1.000 páginas/mes) es más que suficiente; sin ella, esas páginas solo volverán a usar el extractor básico. [firecrawl.dev](https://firecrawl.dev)
- **cuenta de Google** *(opcional)* — la mayoría de los flujos de trabajo pueden escribir en una hoja de cálculo de Google. el modo CSV funciona si prefieres no usarla.

el costo por ejecución está indicado en el README de cada flujo de trabajo.

## por qué existe esto

la mayoría de las herramientas de GTM son productos de suscripción que poseen tus datos, tu lista de prospectos y tus mensajes. pagas $200/asiento/mes por un software que hace lo que podría hacer un buen prompt y un extractor — y no puedes llevarte nada de ello cuando te vas.

esto es lo contrario: flujos de trabajo que puedes leer, modificar, hacer fork y ejecutar en tu propia infraestructura con tus propias claves. cuando Claude mejora, esto mejora. cuando cambias de producto, editas un archivo Markdown.

Claude Code hace todo esto usable sin escribir Python. tu "equipo" es una carpeta.

## qué hay dentro

```
context/
  context.md.example       el cuestionario que Claude Code recorre contigo
  context.md               tu negocio — producto, ICP, competidores, tono de voz (gitignored)

workflows/
  linkedin_outreach/
  email_outreach/
  competitor_analysis/
  content_idea_finder/
  linkedin_comment_helper/
  blog_builder/

scrapers/                  extractores de datos de fuente única (LinkedIn, Twitter, G2…)
skills/                    módulos reutilizables de prompts para Claude
```

cada carpeta de flujo de trabajo tiene su propio README — ábrelo para ver todos los detalles.

## licencia

MIT
