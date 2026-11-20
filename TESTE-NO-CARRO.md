# Guião de teste no carro

Sequência para a próxima sessão. Cada passo diz o que fazer, o que esperar, e
o que muda no código consoante o resultado.

Leva este ficheiro aberto no telemóvel ou imprime-o.

---

## Antes de sair de casa

### 1. Latency timer do FTDI

**É o passo que mais tempo poupa.** O *fast init* do KWP2000 depende de janelas
de 25 ms. O FTDI vem de fábrica com 16 ms de latência, o que chega para
estragar o temporizado e fazer parecer que o protocolo está mal.

```bash
ioreg -r -c IOSerialBSDClient -l | grep -i latency
```

Se der acima de 1, corrige antes de ires. Sem isto vais perseguir um problema
que não existe.

### 2. Código actualizado

```bash
cd ~/ecu-pl
git fetch origin claude/relaxed-galileo-920e6n
git checkout claude/relaxed-galileo-920e6n
git pull origin claude/relaxed-galileo-920e6n
```

### 3. Interruptor do cabo

Se o teu K+DCAN tem interruptor, o E46 é **só linha K** — pinos 7 e 8 em ponte.
A outra posição é para os E9x com D-CAN e não vai comunicar.

### 4. Portátil carregado

Nada aqui grava nada, por isso não há risco de perder a centralina. Mas com a
ignição ligada e o motor parado, uma sessão longa descarrega a bateria do
carro. Se for demorar, leva um carregador de manutenção.

---

## No carro

Arranca o servidor a partir da pasta do projecto:

```bash
cd ~/ecu-pl && python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Todos os passos abaixo são num segundo terminal.

---

### Passo 1 — Confirmar que o básico ainda funciona

**Ignição ligada, motor parado.**

```bash
curl localhost:8000/status
```

Espera-se `"ligado": true`, `"modo": "obd2"` e uma `porta` preenchida.

Repara no **`hz`** — deve andar perto de 1,9. Guarda esse número, é a referência
para comparar depois.

> **Se `ligado` for false:** vê `ultimo_erro`. "sem sync" quase sempre é o
> interruptor do cabo na posição errada, ou a ignição não estar mesmo ligada
> (tem de estar na posição II, não só acessórios).

---

### Passo 2 — Identificar a centralina

**Este é o passo que decide o resto.**

```bash
curl localhost:8000/vehicle
```

Devolve VIN, **Cal ID** e nome da centralina. O Cal ID diz-te se é EDC15 ou
EDC16 sem teres de ir ver a etiqueta debaixo do painel.

> Um 320d de 2004 é normalmente **M47N2 com EDC16C31/C35**. Se confirmar isso,
> muda o endereço e possivelmente a inicialização — anota o que sair.

**Se não responder:** vai ver a etiqueta da centralina fisicamente. Está na
caixa junto ao pára-brisas, lado do condutor. Fotografa a etiqueta.

**Guarda o resultado deste passo.** É a primeira coisa que preciso de ver.

---

### Passo 3 — Descobrir o endereço da DDE

Ainda com o **motor parado**.

```bash
curl -X POST "localhost:8000/kwp/probe?addrs=0x12&inits=fast,slow"
```

Fecha a ligação, tenta cada combinação, volta a ligar. O painel pisca alguns
segundos — é esperado.

**Se nenhuma pegar,** alarga:

```bash
curl -X POST "localhost:8000/kwp/probe?addrs=0x12,0x11,0x10,0x01&inits=fast,slow"
```

Procura na resposta a linha com `"ok": true`. O campo `ecuid` confirma que
falaste mesmo com a centralina.

**Guarda o resultado.** É a segunda coisa que preciso de ver.

---

### Passo 4 — Passar a KWP2000

Com o endereço e a inicialização que funcionaram no passo 3:

```bash
curl -X POST "localhost:8000/mode/kwp?addr=0x12&init=fast"
curl localhost:8000/status
```

Duas coisas a verificar:

1. **`modo` ficou em `kwp`?** Se voltou a `obd2` sozinho, a sessão não subiu —
   vê `ultimo_erro` e volta ao passo 3.
2. **`canais_activos` tem alguma coisa?** Se sim, a DDE aceita PIDs OBD2 dentro
   da sessão KWP e o painel continua a funcionar. Se estiver vazio, é normal —
   os blocos ainda não estão mapeados.

---

### Passo 5 — Retratos dos blocos

**Motor parado, ignição ligada:**

```bash
curl -X POST "localhost:8000/kwp/scan?label=ignicao" > scan-ignicao.json
```

Demora alguns minutos — são 255 pedidos. Vai buscar um café.

**Agora liga o motor e deixa ao ralenti:**

```bash
curl -X POST "localhost:8000/kwp/scan?label=ralenti&force=true" > scan-ralenti.json
```

O `force=true` é preciso porque o servidor recusa varrer com o motor a
trabalhar — o varrimento demora e ocupa o barramento, por isso não é para
fazer em andamento. Ao ralenti parado é seguro.

**Se conseguires, um terceiro a rotação mais alta.** Precisas de alguém a
segurar o acelerador a ~2500 rpm durante o varrimento, ou é capaz de não valer
a pena. Se der:

```bash
curl -X POST "localhost:8000/kwp/scan?label=2500&force=true" > scan-2500.json
```

Depois compara:

```bash
curl "localhost:8000/kwp/diff?a=ignicao&b=ralenti" > diff-1.json
curl "localhost:8000/kwp/diff?a=ralenti&b=2500" > diff-2.json
```

**Guarda os quatro ficheiros.** São a terceira e mais importante coisa que
preciso de ver — é a partir deles que se preenche o mapa de canais.

---

### Passo 6 — Transitório do acelerador

**Precisa de duas pessoas, ou de um sítio onde possas parar em segurança.**
Não faças isto a conduzir sozinho com o computador ao colo.

Motor à temperatura de serviço:

```bash
curl -X POST "localhost:8000/trace/arm?threshold=25&seconds=4"
```

Acelera a fundo em 2ª ou 3ª a partir de rotação baixa. A captura dispara
sozinha quando o pedal passa os 25%.

```bash
curl localhost:8000/trace > trace-1.json
```

A resposta traz a análise: quando as rotações subiram, quando a pressão começou
a subir, quanto tempo até 90% do máximo, e notas sobre o que isso sugere.

Faz **duas ou três** capturas para veres se são consistentes.

> **Ritmo esperado:** 3 a 4 Hz. Se o campo `ritmo_hz` da análise vier abaixo de
> 3, é sinal de que o latency timer não ficou corrigido.

---

## O que trazer de volta

Por ordem de importância:

| # | Ficheiro / saída | Para quê |
|---|---|---|
| 1 | `curl localhost:8000/vehicle` | Identifica a centralina — decide tudo o resto |
| 2 | Resultado do `/kwp/probe` | Endereço e inicialização que funcionam |
| 3 | `scan-*.json` e `diff-*.json` | Preencher o mapa de canais |
| 4 | `trace-*.json` | Diagnóstico da resposta do pedal |
| 5 | `curl localhost:8000/status` em cada fase | Ritmo e erros |

Cola-os na sessão seguinte, ou mete-os numa pasta e diz-me onde estão.

---

## O que vamos alterar na próxima sessão

Consoante o que os passos derem:

### Se o passo 2 confirmar EDC16

O endereço e o formato mudam. Vou ajustar o cliente KWP e possivelmente
acrescentar suporte a `0x22` (ReadDataByCommonIdentifier), que as EDC16 usam
mais do que o `0x21`.

### Se o passo 3 falhar em todos os endereços

Duas hipóteses, e trato as duas: a inicialização precisa de parâmetros
diferentes, ou a DDE não fala KWP no barramento OBD e só responde por DS2. Nesse
caso implemento o DS2, que é o outro protocolo da BMW nesta geração.

### Se o passo 5 correr bem

**É aqui que está o trabalho principal.** Com os diffs, identifico os canais e
preencho a tabela `MEAS` — pressão do rail pedida e real, massa de ar pedida e
real, correcções por injector, posição do EGR, comando VNT.

Depois disso:

- O ritmo sobe de ~1,9 Hz para 4 ou 5 Hz, porque cada bloco traz vários canais
  num pedido só
- O painel ganha as barras de pedido‑contra‑real
- O ecrã de diagnóstico deixa de explicar o P0263 e passa a **apontar o
  injector**, com o número a desviar-se em directo

### Se o passo 6 mostrar atraso

Com os canais mapeados, o `/trace` passa a incluir a pressão **pedida** ao lado
da real. Aí a análise deixa de ser "a pressão demorou" e passa a ser "a
centralina pediu 1,4 bar aos 0,3 s e só recebeu aos 1,8 s" — que é o que
distingue palhetas presas de fuga no intercooler.

### Independentemente do resto

- Levar o painel bom que tens no Mac para o repositório, com os manómetros
  configuráveis e o diagnóstico ilustrado
- Ligar as barras novas aos canais reais

---

## Se alguma coisa correr mal

Nada neste servidor consegue inutilizar a centralina — não há gravação de
flash, não há *security access*, não há escrita em calibrações. Ver `RISCOS.md`
para a tabela completa.

O pior desfecho realista é um erro de comunicação. Se acontecer:

```bash
curl localhost:8000/status          # vê ultimo_erro
curl -X POST localhost:8000/mode/obd2   # volta ao modo que se sabe que funciona
```

Se a centralina ficar estranha depois de uma sessão, desliga a ignição e espera
30 segundos. A sessão de diagnóstico expira sozinha.
