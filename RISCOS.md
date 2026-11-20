# Riscos — o que este servidor pode e não pode estragar

## A resposta curta

**Nada neste servidor consegue inutilizar a centralina.**

Não há gravação de flash, não há *security access*, não há escrita em EEPROM,
não há alteração de calibrações. O código não sabe fazer nada disso, e não é
por omissão — é por decisão.

O único serviço que escreve alguma coisa é o `0x14`, que apaga a memória de
avarias. Apaga registos de diagnóstico, não código nem calibrações.

O pior desfecho realista de tudo o resto é uma trama malformada que a
centralina ignora, ou um erro de comunicação que gera um código do tipo
"mensagem perdida no barramento" — e que se apaga a seguir.

## O que estraga centralinas, e que aqui não existe

| Operação | Risco | Neste servidor |
|---|---|---|
| Gravar flash | **Inutiliza** se a tensão cair a meio | Não existe |
| Escrever calibrações | Motor a trabalhar mal, recuperável | Não existe |
| Códigos IMA de injectores | Erro de injecção, recuperável | Não existe |
| Codificação de módulos | Funções perdidas, recuperável | Não existe |
| Apagar avarias | Perdes os dados congelados | `POST /kwp/cleardtc` |
| Ler blocos, PIDs, avarias | Nenhum | Tudo o resto |

Todas as três primeiras linhas exigem passar o serviço `0x27`, cuja função
*seed/key* é da BMW e não está implementada aqui.

## Salvaguardas no código

**O varrimento recusa com o motor a trabalhar.** `POST /kwp/scan` percorre 255
identificadores e demora minutos, ocupando o barramento todo esse tempo. Com o
motor parado e a ignição ligada não há problema nenhum. A trabalhar, não faz
sentido — e o servidor devolve 409. `force=true` insiste, se souberes o que
estás a fazer.

**O varrimento só usa `0x21`.** É serviço de leitura, e o número do serviço
está fixo no código. Não há caminho por onde um identificador mal escrito se
transforme num serviço de escrita.

**Apagar avarias exige confirmação.** `confirm=true` obrigatório, porque limpar
antes de diagnosticar deita fora os dados congelados que acompanham cada
código — muitas vezes a informação mais útil que tens.

**A sessão fecha-se em condições.** Ao mudar de modo ou ao perder a ligação, o
cliente envia `StopCommunication` (`0x82`). Sem isto algumas centralinas ficam
à espera do tester e só voltam ao normal ao fim de um tempo.

**Se o KWP não pegar, volta a OBD2.** Não ficas sem painel por causa de uma
tentativa falhada.

## Cuidados que continuam a ser teus

**Bateria.** Nada aqui grava nada, por isso não há risco de perder a centralina
por queda de tensão. Mas com a ignição ligada e o motor parado, uma sessão
longa de varrimento descarrega a bateria. Se fores fazer isso mais do que uns
minutos, liga um carregador de manutenção.

**O carro a andar.** Não faças varrimentos nem experiências com o carro em
movimento. A captura de transitório (`/trace`) é a excepção — foi feita para
isso — mas faz-se com alguém ao volante e o computador fechado, ou numa estrada
onde possas acelerar em segurança.

**Se um dia gravares flash noutra ferramenta:** carregador ligado durante todo
o processo, e o ficheiro original lido e guardado antes de escreveres. Tensão a
cair a meio de uma gravação é a forma mais comum de perder uma centralina, e
recuperar exige tirá-la do carro e ir por BDM.

## Resposta ao acelerador

O `/trace` mede, não altera.

Mudar a resposta do pedal é reescrever o mapa no flash — está fora do que este
servidor faz, e é onde o risco real vive. O que o `/trace` dá é a decomposição
do atraso: quando o pedal foi a fundo, quando as rotações começaram a subir,
quando a pressão começou a subir, e quanto tempo levou a chegar perto do
máximo.

Isso diz-te se o problema é o mapa ou se é uma avaria a fingir de mapa. Num
motor com 20 anos, é quase sempre a segunda.

**Limitação honesta:** a linha K a 10400 baud dá cerca de 3 a 4 Hz durante a
captura, mesmo lendo só três canais. Para um transitório de dois segundos são
seis a oito amostras. Chega para ver a forma e para comparar antes e depois de
uma limpeza, não chega para números absolutos com precisão.

Se quiseres pedal mais vivo sem tocar na centralina, existem *pedal boosters* —
caixas que reescalam o sinal do pedal antes de chegar à ECU. Não dão potência,
só mudam o tacto, e desligam-se quando quiseres.
