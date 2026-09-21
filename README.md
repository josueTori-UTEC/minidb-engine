# MiniDB — Motor Relacional en Disco

MiniDB es un sistema gestor de base de datos relacional desarrollado como proyecto del curso Base de Datos II.

El objetivo del proyecto es implementar desde cero algunos de los principales componentes internos de un SGBD, trabajando directamente con archivos binarios y operaciones de entrada/salida en disco.

## Entregable 1

Para la primera entrega se desarrollarán los siguientes componentes:

* Almacenamiento paginado en disco.
* Identificación de registros mediante RID.
* Heap File.
* Sequential File con área de overflow y reorganización.
* Árbol B+ persistente en disco.
* Extendible Hashing persistente en disco.
* Parser SQL básico.
* Query Planner y Executor.
* API REST.
* Cliente web para ejecutar consultas.
* Métricas de lecturas y escrituras en disco.
* Experimentos de rendimiento.

## Tecnologías

### Backend

* Python
* FastAPI

### Frontend

* React
* TypeScript

### Persistencia

Los datos e índices serán almacenados directamente en archivos binarios utilizando operaciones de lectura y escritura sobre disco.

## Arquitectura general

```text
Cliente Web
     |
     v
API REST
     |
     v
Parser SQL
     |
     v
Planner / Executor
     |
     +----------------------+
     |          |           |
     v          v           v
Heap File   Sequential   Índices
               File      B+ / Hash
     \          |          /
      \         |         /
       v        v        v
         Storage Manager
               |
               v
         Archivos binarios
```

## Estado del proyecto

Actualmente el proyecto se encuentra en la etapa inicial de diseño e implementación del núcleo de almacenamiento.

El primer componente a desarrollar será el Storage Manager, encargado de representar páginas, registros, RIDs y operaciones físicas de lectura y escritura.
