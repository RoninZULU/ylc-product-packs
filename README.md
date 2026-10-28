# Paquetes de productos de Your Last Coach

Bases SQLite con los productos envasados más escaneados de cada país, para
que la app [Your Last Coach](https://providencehq.es) los encuentre al
instante y sin conexión. Se regeneran solas cada mes con GitHub Actions y se
publican como [la última versión](../../releases/latest).

| País | Qué lleva |
| --- | --- |
| `es` | Productos de Open Food Facts escaneados en España este año y el anterior, con tabla nutricional |
| `us` | Lo mismo en EE. UU. (los 60.000 más escaneados) y los productos vigentes de USDA FoodData Central (Branded Foods) que Open Food Facts no tiene con tabla |

La app baja `index.json` y el paquete de su país (`<país>.sqlite.gz`) con
wifi, comprueba su SHA-256 y lo cambia entero: nunca se acumulan. Con red,
manda siempre la consulta en directo a Open Food Facts.

## Formato

Tabla `p`, una fila por código de barras (EAN-13, EAN-8 o UPC-A con un cero
delante). Los nutrientes son por 100 g o 100 ml y van como enteros
multiplicados por la escala de `index.json` (`scale`): `kcal` × 10, macros
× 100, sal × 1000, vitaminas y minerales en gramos × 10⁵ (hierro × 10⁶).
`src` es 0 para Open Food Facts y 1 para USDA. `cats` sólo lleva las
categorías que usa la app. `img` es el nombre de la foto en la carpeta del
producto en `images.openfoodfacts.org`.

## Cómo se genera

```sh
python build_packs.py --off en.openfoodfacts.org.products.csv.gz \
  --usda FoodData_Central_branded_food_csv_AAAA-MM-DD.zip --out dist
```

Se descartan las tablas imposibles (kJ apuntados como kcal, macros que no
caben en 100 g) y los códigos internos (siete ceros delante).

## Licencias

- Datos de **Open Food Facts** (https://openfoodfacts.org): [Open Database
  License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/); cada
  producto, [Database Contents License (DbCL)
  1.0](https://opendatacommons.org/licenses/dbcl/1-0/). Estos paquetes son una
  base derivada y se publican con la misma licencia.
- Datos de **USDA FoodData Central**: dominio público (CC0 1.0).
- El script (`build_packs.py`): MIT.
